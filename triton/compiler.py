from __future__ import annotations

import ast
import contextlib
import functools
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import warnings
from collections import namedtuple
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Type, Union

import setuptools
import torch
from filelock import FileLock

import triton
import triton._C.libtriton.triton as _triton
from . import impl
from .tools.disasm import extract


def static_vars(**kwargs):
    def decorate(func):
        for k in kwargs:
            setattr(func, k, kwargs[k])
        return func
    return decorate


def str_to_ty(name):
    if name[0] == "*":
        ty = str_to_ty(name[1:])
        return triton.language.pointer_type(ty)
    tys = {
        "fp8e5": triton.language.float8e5,
        "fp8e4": triton.language.float8e4,
        "fp16": triton.language.float16,
        "bf16": triton.language.bfloat16,
        "fp32": triton.language.float32,
        "fp64": triton.language.float64,
        "i1": triton.language.int1,
        "i8": triton.language.int8,
        "i16": triton.language.int16,
        "i32": triton.language.int32,
        "i64": triton.language.int64,
        "u8": triton.language.uint8,
        "u16": triton.language.uint16,
        "u32": triton.language.uint32,
        "u64": triton.language.uint64,
        "B": triton.language.int1,
    }
    return tys[name]


def mangle_ty(ty):
    if ty.is_ptr():
        return 'P' + mangle_ty(ty.element_ty)
    if ty.is_int():
        SIGNED = triton.language.dtype.SIGNEDNESS.SIGNED
        prefix = 'i' if ty.int_signedness == SIGNED else 'u'
        return prefix + str(ty.int_bitwidth)
    if ty.is_fp8():
        return 'fp8'
    if ty.is_fp16():
        return 'fp16'
    if ty.is_bf16():
        return 'bf16'
    if ty.is_fp32():
        return 'fp32'
    if ty.is_fp64():
        return 'fp64'
    if ty.is_block():
        elt = mangle_ty(ty.scalar)
        shape = '_'.join(map(str, ty.shape))
        return f'{elt}S{shape}S'
    if ty.is_void():
        return 'V'
    assert False, "Unsupported type"


def mangle_fn(name, arg_tys, constants):
    # doesn't mangle ret type, which must be a function of arg tys
    mangled_arg_names = '_'.join([mangle_ty(ty) for ty in arg_tys])
    mangled_constants = '_'.join([f'{i}c{repr(constants[i])}' for i in sorted(constants)])
    mangled_constants = mangled_constants.replace('.', '_d_')
    mangled_constants = mangled_constants.replace("'", '_sq_')
    ret = f'{name}__{mangled_arg_names}__{mangled_constants}'
    return ret


def _is_triton_tensor(o: Any) -> bool:
    return isinstance(o, triton.language.tensor)


def _is_constexpr(o: Any) -> bool:
    return isinstance(o, triton.language.constexpr)  # TODO: fetch triton.language.constexpr to a global after circular imports untangled, saving getattr


def _unwrap_if_constexpr(o: Any):
    return o.value if isinstance(o, triton.language.constexpr) else o


_condition_types = {bool, int, type(None)}  # Python types accepted for conditionals inside kernels


class enter_sub_region:
    def __init__(self, generator: CodeGenerator):
        self.generator = generator

    def __enter__(self):
        # record lscope & local_defs in the parent scope
        self.liveins = self.generator.lscope.copy()
        self.prev_defs = self.generator.local_defs.copy()
        self.generator.local_defs = {}
        self.insert_block = self.generator.builder.get_insertion_block()
        self.insert_point = self.generator.builder.get_insertion_point()
        return self.liveins, self.insert_block

    def __exit__(self, *args, **kwargs):
        self.generator.builder.restore_insertion_point(self.insert_point)
        self.generator.lscope = self.liveins
        self.generator.local_defs = self.prev_defs


class CodeGenerator(ast.NodeVisitor):
    def __init__(self, context, prototype, gscope, attributes, constants, function_name,
                 module=None, is_kernel=False, function_types: Optional[Dict] = None, debug=False):
        self.builder = _triton.ir.builder(context)
        self.module = self.builder.create_module() if module is None else module
        self.function_ret_types = {} if function_types is None else function_types
        self.prototype = prototype
        self.gscope = gscope
        self.lscope = dict()
        self.attributes = attributes
        self.constants = constants
        self.function_name = function_name
        self.is_kernel = is_kernel
        self.last_node = None
        self.debug = debug
        self.scf_stack = []
        # SSA-construction
        # name => triton.language.tensor
        self.local_defs: Dict[str, triton.language.tensor] = {}
        self.global_uses: Dict[str, triton.language.tensor] = {}
        self.dereference_name: Callable[[str], Any] = self._define_name_lookup()

    builtin_namespace: Dict[str, Any] = {_.__name__: _ for _ in (range, float, int, isinstance, getattr)}

    def _define_name_lookup(self):
        # TODO: this needs to be moved to class scope when cyclic imports untangled and `triton.language` can be imported at module level
        self.builtin_namespace.update((
            ('print', triton.language.core.device_print),
            ('min', triton.language.minimum),  # TODO: why `min`? if `min`, why not `max`? `sum`? `all`?
        ))
        # TODO: this needs to be moved to class scope when cyclic imports untangled and `triton.language` can be imported at module level
        self.statically_implemented_functions.update((
            (triton.language.core.static_assert, CodeGenerator.execute_static_assert),
            (triton.language.core.static_print, CodeGenerator.execute_static_print),
        ))

        def local_lookup(name: str, absent):
            value = self.lscope.get(name, absent)  # this needs to be re-fetched from `self` every time, because it gets switched occasionally
            if value is not absent and name not in self.local_defs:
                self.global_uses[name] = value
            return value

        absent_marker = object()

        def name_lookup(name: str) -> Any:
            lookup_order = local_lookup, self.gscope.get, self.builtin_namespace.get
            absent = absent_marker
            for lookup_function in lookup_order:
                value = lookup_function(name, absent)
                if value is not absent:
                    return value
            raise NameError(f'{name} is not defined')

        return name_lookup

    def set_value(self, name: str,
                  value: Union[triton.language.tensor, triton.language.constexpr]) -> None:
        ''' This function:
          called by visit_Assign() & visit_FuncDef() to store left value (lvalue)
        1. record local defined name (FIXME: should consider control flow)
        2. store tensor in self.lvalue
        '''
        self.lscope[name] = value
        self.local_defs[name] = value

    #
    # AST visitor
    #
    def visit_compound_statement(self, stmts):
        for stmt in stmts:
            self.last_ret_type = self.visit(stmt)
            if isinstance(stmt, ast.Return):
                break
        return stmts and isinstance(stmt, ast.Return)

    # TODO: should be its own AST visitor
    def contains_return_op(self, node):
        if isinstance(node, ast.Return):
            return True
        elif isinstance(node, ast.Assign):
            return self.contains_return_op(node.value)
        elif isinstance(node, ast.Module):
            pred = lambda s: self.contains_return_op(s)
            return any(pred(s) for s in node.body)
        elif isinstance(node, ast.FunctionDef):
            pred = lambda s: self.contains_return_op(s)
            return any(pred(s) for s in node.body)
        elif isinstance(node, ast.Call):
            fn = self.visit(node.func)
            if isinstance(fn, triton.JITFunction):
                old_gscope = self.gscope
                self.gscope = sys.modules[fn.fn.__module__].__dict__
                ret = self.contains_return_op(fn.parse())
                self.gscope = old_gscope
                return ret
            return False
        elif isinstance(node, ast.If):
            pred = lambda s: self.contains_return_op(s)
            ret = any(pred(s) for s in node.body)
            if node.orelse:
                ret = ret or any(pred(s) for s in node.orelse)
            return ret
        else:
            return False

    def visit_Module(self, node):
        ast.NodeVisitor.generic_visit(self, node)

    def visit_List(self, node):
        ctx = self.visit(node.ctx)
        assert ctx is None
        elts = [self.visit(elt) for elt in node.elts]
        return elts

    # By design, only non-kernel functions can return
    def visit_Return(self, node):
        ret_value = self.visit(node.value)
        # ret_block = self.builder.create_block()
        # post_ret_block = self.builder.create_block()
        # self.builder.create_branch(ret_block)
        # self.builder.set_insertion_point_to_end(ret_block)
        if ret_value is None:
            self.builder.ret([])
            ret_ty = None
        elif isinstance(ret_value, tuple):
            ret_values = [triton.language.core._to_tensor(v, self.builder) for v in ret_value]
            ret_types = [v.type for v in ret_values]
            self.builder.ret([v.handle for v in ret_values])
            ret_ty = tuple(ret_types)
        else:
            ret = triton.language.core._to_tensor(ret_value, self.builder)
            self.builder.ret([ret.handle])
            ret_ty = ret.type
        # self.builder.create_branch(post_ret_block)
        # self.builder.set_insertion_point_to_end(post_ret_block)
        return ret_ty

    def visit_FunctionDef(self, node):
        arg_names, kwarg_names = self.visit(node.args)
        # initialize defaults
        for i, default_value in enumerate(node.args.defaults):
            arg_node = node.args.args[-i - 1]
            annotation = arg_node.annotation
            name = arg_node.arg
            st_target = ast.Name(id=name, ctx=ast.Store())
            if annotation is None:
                init_node = ast.Assign(targets=[st_target], value=default_value)
            else:
                init_node = ast.AnnAssign(target=st_target, value=default_value, annotation=annotation)
            self.visit(init_node)
        # initialize function
        visibility = "public" if self.is_kernel else "private"
        fn = self.builder.get_or_insert_function(self.module, self.function_name, self.prototype.to_ir(self.builder), visibility)
        self.module.push_back(fn)
        entry = fn.add_entry_block()
        arg_values = []
        idx = 0
        for i, arg_name in enumerate(arg_names):
            if i in self.constants:
                cst = self.constants[i]
                if not _is_constexpr(cst):
                    cst = triton.language.constexpr(self.constants[i])
                arg_values.append(cst)
                continue
            else:
                if i in self.attributes:
                    fn.set_arg_attr(idx, "tt.divisibility", self.attributes[i][1])
                arg_values.append(triton.language.tensor(fn.args(idx), self.prototype.param_types[idx]))
                idx += 1

        insert_pt = self.builder.get_insertion_block()
        for arg_name, arg_value in zip(arg_names, arg_values):
            self.set_value(arg_name, arg_value)
        self.builder.set_insertion_point_to_start(entry)
        # visit function body
        has_ret = self.visit_compound_statement(node.body)
        # finalize function
        if not has_ret:
            self.builder.ret([])
        else:
            # update return type
            if isinstance(self.last_ret_type, tuple):
                self.prototype.ret_types = list(self.last_ret_type)
                fn.reset_type(self.prototype.to_ir(self.builder))
            else:
                self.prototype.ret_types = [self.last_ret_type]
                fn.reset_type(self.prototype.to_ir(self.builder))
        if insert_pt:
            self.builder.set_insertion_point_to_end(insert_pt)

    def visit_arguments(self, node):
        arg_names = []
        for arg in node.args:
            arg_names += [self.visit(arg)]
        kwarg_names = self.visit(node.kwarg)
        return arg_names, kwarg_names

    def visit_arg(self, node):
        ast.NodeVisitor.generic_visit(self, node)
        return node.arg

    def visit_AnnAssign(self, node):
        # extract attributes
        annotation = self.visit(node.annotation)
        target = self.visit(node.target)
        value = self.visit(node.value)
        # constexpr
        if annotation == triton.language.constexpr:
            if target in self.lscope:
                raise ValueError(f'{target} is already defined.'
                                 f' constexpr cannot be reassigned.')
            if not _is_constexpr(value):
                value = triton.language.constexpr(value)
            self.lscope[target] = value
            return self.lscope[target]
        # default: call visit_Assign
        return self.visit_Assign(node)

    def visit_Assign(self, node):
        _names = []
        for target in node.targets:
            _names += [self.visit(target)]
        if len(_names) > 1:
            raise UnsupportedLanguageConstruct(None, node, "simultaneous multiple assignment is not supported.")
        names = _names[0]
        values = self.visit(node.value)
        if not isinstance(names, tuple):
            names = [names]
        if not isinstance(values, tuple):
            values = [values]
        native_nontensor_types = (triton.language.dtype, )
        for name, value in zip(names, values):
            # by default, constexpr are assigned into python variable
            value = _unwrap_if_constexpr(value)
            if not _is_triton_tensor(value) and \
               not isinstance(value, native_nontensor_types):
                value = triton.language.core._to_tensor(value, self.builder)
            self.set_value(name, value)

    def visit_AugAssign(self, node):
        name = node.target.id
        lhs = ast.Name(id=name, ctx=ast.Load())
        rhs = ast.BinOp(lhs, node.op, node.value)
        assign = ast.Assign(targets=[node.target], value=rhs)
        self.visit(assign)
        return self.dereference_name(name)

    def visit_Name(self, node):
        if type(node.ctx) == ast.Store:
            return node.id
        return self.dereference_name(node.id)

    def visit_Store(self, node):
        ast.NodeVisitor.generic_visit(self, node)

    def visit_Load(self, node):
        ast.NodeVisitor.generic_visit(self, node)

    def visit_Tuple(self, node):
        args = [self.visit(x) for x in node.elts]
        return tuple(args)

    def _apply_binary_method(self, method_name, lhs, rhs):
        # TODO: raise something meaningful if getattr fails below, esp for reverse method
        if _is_triton_tensor(lhs):
            return getattr(lhs, method_name)(rhs, _builder=self.builder)
        if _is_triton_tensor(rhs):
            reverse_method_name = re.sub(r"__(.*)__", r"__r\1__", method_name)
            return getattr(rhs, reverse_method_name)(lhs, _builder=self.builder)
        return getattr(lhs, method_name)(rhs)

    def visit_BinOp(self, node):
        lhs = self.visit(node.left)
        rhs = self.visit(node.right)
        method_name = self._method_name_for_bin_op.get(type(node.op))
        if method_name is None:
            raise UnsupportedLanguageConstruct(None, node, "AST binary operator '{}' is not (currently) implemented.".format(node.op.__name__))
        return self._apply_binary_method(method_name, lhs, rhs)
    _method_name_for_bin_op: Dict[Type[ast.operator], str] = {
        ast.Add: '__add__', ast.Sub: '__sub__', ast.Mult: '__mul__', ast.Div: '__truediv__',
        ast.FloorDiv: '__floordiv__', ast.Mod: '__mod__', ast.Pow: '__pow__',
        ast.LShift: '__lshift__', ast.RShift: '__rshift__', ast.BitAnd: '__and__', ast.BitOr: '__or__', ast.BitXor: '__xor__',
    }

    def visit_then_else_blocks(self, node, liveins, then_block, else_block):
        # then block
        self.builder.set_insertion_point_to_start(then_block)
        self.visit_compound_statement(node.body)
        then_block = self.builder.get_insertion_block()
        then_defs = self.local_defs.copy()
        # else block
        else_defs = {}
        if node.orelse:
            self.builder.set_insertion_point_to_start(else_block)
            self.lscope = liveins.copy()
            self.local_defs = {}
            self.visit_compound_statement(node.orelse)
            else_defs = self.local_defs.copy()
            else_block = self.builder.get_insertion_block()

        # update block arguments
        names = []
        ret_types = []
        ir_ret_types = []
        # variables in livein whose value is updated in `if`
        for name in liveins:
            # check type
            for defs, block_name in [(then_defs, 'then'), (else_defs, 'else')]:
                if name in defs:
                    assert defs[name].type == liveins[name].type,\
                        f'initial value for `{name}` is of type {liveins[name].type}, '\
                        f'but the {block_name} block redefines it as {defs[name].type}'
            if name in then_defs or name in else_defs:
                names.append(name)
                ret_types.append(then_defs[name].type if name in then_defs else else_defs[name].type)
                ir_ret_types.append(then_defs[name].handle.get_type() if name in then_defs else else_defs[name].handle.get_type())
            # variable defined in then but not in else
            if name in then_defs and name not in else_defs:
                else_defs[name] = liveins[name]
            # variable defined in else but not in then
            if name in else_defs and name not in then_defs:
                then_defs[name] = liveins[name]
        # variables that are both in then and else but not in liveins
        # TODO: could probably be cleaned up
        for name in then_defs.keys() & else_defs.keys():
            if name in names:
                continue
            then_ty = then_defs[name].type
            else_ty = else_defs[name].type
            assert then_ty == else_ty,\
                f'mismatched type for {name} between then block ({then_ty}) '\
                f'and else block ({else_ty})'
            names.append(name)
            ret_types.append(then_ty)
            ir_ret_types.append(then_defs[name].handle.get_type())

        return then_defs, else_defs, then_block, else_block, names, ret_types, ir_ret_types

    def visit_if_top_level(self, cond, node):
        with enter_sub_region(self) as sr:
            liveins, ip_block = sr
            then_block = self.builder.create_block()
            else_block = self.builder.create_block()
            # create basic-block after conditional
            endif_block = self.builder.create_block()
            # create branch
            self.builder.set_insertion_point_to_end(ip_block)
            self.builder.create_cond_branch(cond.handle, then_block, else_block)
            # visit then and else blocks
            then_defs, else_defs, then_block, else_block, names, ret_types, ir_ret_types = \
                self.visit_then_else_blocks(node, liveins, then_block, else_block)
            # then terminator
            self.builder.set_insertion_point_to_end(then_block)
            if not then_block.has_terminator():
                self.builder.create_branch(endif_block, [then_defs[n].handle for n in names])
            # else terminator
            self.builder.set_insertion_point_to_end(else_block)
            if not else_block.has_terminator():
                self.builder.create_branch(endif_block, [else_defs[n].handle for n in names])
            for ty in ir_ret_types:
                endif_block.add_argument(ty)
        # change block
        self.builder.set_insertion_point_to_start(endif_block)
        # update value
        for i, name in enumerate(names):
            new_tensor = triton.language.core.tensor(endif_block.arg(i), ret_types[i])
            self.set_value(name, new_tensor)

    # TODO: refactor
    def visit_if_scf(self, cond, node):
        with enter_sub_region(self) as sr:
            liveins, _ = sr
            ip = self.builder.get_insertion_point()
            then_block = self.builder.create_block()
            else_block = self.builder.create_block() if node.orelse else None
            then_defs, else_defs, then_block, else_block, names, ret_types, _ = \
                self.visit_then_else_blocks(node, liveins, then_block, else_block)
            # create if op
            self.builder.restore_insertion_point(ip)
            if_op = self.builder.create_if_op([ty.to_ir(self.builder) for ty in ret_types], cond.handle, True)
            then_block.merge_block_before(if_op.get_then_block())
            self.builder.set_insertion_point_to_end(if_op.get_then_block())
            if len(names) > 0:
                self.builder.create_yield_op([then_defs[n].handle for n in names])
            if not node.orelse:
                else_block = if_op.get_else_block()
            else:
                else_block.merge_block_before(if_op.get_else_block())
            self.builder.set_insertion_point_to_end(if_op.get_else_block())
            if len(names) > 0:
                self.builder.create_yield_op([else_defs[n].handle for n in names])
        # update values
        for i, name in enumerate(names):
            new_tensor = triton.language.core.tensor(if_op.get_result(i), ret_types[i])
            self.set_value(name, new_tensor)

    def visit_If(self, node):
        cond = self.visit(node.test)
        if _is_triton_tensor(cond):
            cond = cond.to(triton.language.int1, _builder=self.builder)
            if self.scf_stack or not self.contains_return_op(node):
                self.visit_if_scf(cond, node)
            else:
                self.visit_if_top_level(cond, node)
        else:
            cond = _unwrap_if_constexpr(cond)
            if type(cond) not in _condition_types:  # not isinstance - we insist the real thing, no subclasses and no ducks
                raise UnsupportedLanguageConstruct(
                    None, node, "`if` conditionals can only accept values of type {{{}}}, not objects of type {}".format(
                        ', '.join(_.__name__ for _ in _condition_types), type(cond).__name__))
            if cond:
                self.visit_compound_statement(node.body)
            else:
                self.visit_compound_statement(node.orelse)

    def visit_IfExp(self, node):
        cond = self.visit(node.test)
        if cond.value:
            return self.visit(node.body)
        else:
            return self.visit(node.orelse)

    def visit_Pass(self, node):
        pass

    def visit_Compare(self, node):
        if not (len(node.comparators) == 1 and len(node.ops) == 1):
            raise UnsupportedLanguageConstruct(None, node, "simultaneous multiple comparison is not supported")
        lhs = _unwrap_if_constexpr(self.visit(node.left))
        rhs = _unwrap_if_constexpr(self.visit(node.comparators[0]))
        if type(node.ops[0]) == ast.Is:
            return triton.language.constexpr(lhs is rhs)
        if type(node.ops[0]) == ast.IsNot:
            return triton.language.constexpr(lhs is not rhs)
        method_name = self._method_name_for_comp_op.get(type(node.ops[0]))
        if method_name is None:
            raise UnsupportedLanguageConstruct(None, node, "AST comparison operator '{}' is not (currently) implemented.".format(node.ops[0].__name__))
        return self._apply_binary_method(method_name, lhs, rhs)
    _method_name_for_comp_op: Dict[Type[ast.cmpop], str] = {
        ast.Eq: '__eq__', ast.NotEq: '__ne__', ast.Lt: '__lt__', ast.LtE: '__le__', ast.Gt: '__gt__', ast.GtE: '__ge__'
    }

    def visit_UnaryOp(self, node):
        op = self.visit(node.operand)
        fn = self._method_name_for_unary_op.get(type(node.op))
        if fn is None:
            raise UnsupportedLanguageConstruct(None, node, "AST unary operator '{}' is not (currently) implemented.".format(node.op.__name__))
        if _is_triton_tensor(op):
            return getattr(op, fn)(_builder=self.builder)
        return getattr(op, fn)()
    _method_name_for_unary_op: Dict[Type[ast.unaryop], str] = {ast.USub: '__neg__', ast.UAdd: '__pos__', ast.Not: '__not__', ast.Invert: '__invert__'}

    def visit_While(self, node):
        with enter_sub_region(self) as sr:
            liveins, insert_block = sr

            # loop body (the after region)
            # loop_block = self.builder.create_block()
            dummy = self.builder.create_block()
            self.builder.set_insertion_point_to_start(dummy)
            self.scf_stack.append(node)
            self.visit_compound_statement(node.body)
            self.scf_stack.pop()
            loop_defs = self.local_defs

            # collect loop-carried values
            names = []
            ret_types = []
            init_args = []
            for name in loop_defs:
                if name in liveins:
                    # We should not def new constexpr
                    assert _is_triton_tensor(loop_defs[name])
                    assert _is_triton_tensor(liveins[name])
                    assert loop_defs[name].type == liveins[name].type
                    # these are loop-carried values
                    names.append(name)
                    ret_types.append(loop_defs[name].type)
                    init_args.append(liveins[name])

            self.builder.set_insertion_point_to_end(insert_block)
            while_op = self.builder.create_while_op([ty.to_ir(self.builder) for ty in ret_types],
                                                    [arg.handle for arg in init_args])
            # merge the condition region
            before_block = self.builder.create_block_with_parent(while_op.get_before(),
                                                                 [ty.to_ir(self.builder) for ty in ret_types])
            self.builder.set_insertion_point_to_start(before_block)
            for i, name in enumerate(names):
                self.lscope[name] = triton.language.core.tensor(before_block.arg(i), ret_types[i])
                self.local_defs[name] = self.lscope[name]
            cond = self.visit(node.test)
            self.builder.set_insertion_point_to_end(before_block)
            # create ConditionOp: e.g., scf.condition(%cond) %arg0, %arg1, ...
            self.builder.create_condition_op(cond.handle, [before_block.arg(i) for i in range(len(init_args))])
            # merge the loop body
            after_block = self.builder.create_block_with_parent(while_op.get_after(),
                                                                [ty.to_ir(self.builder) for ty in ret_types])

            # generate loop body
            self.builder.set_insertion_point_to_start(after_block)
            for i, name in enumerate(names):
                self.lscope[name] = triton.language.core.tensor(after_block.arg(i), ret_types[i])
                self.local_defs[name] = self.lscope[name]
            self.scf_stack.append(node)
            self.visit_compound_statement(node.body)
            self.scf_stack.pop()
            loop_defs = self.local_defs
            yields = []
            for name in loop_defs:
                if name in liveins:
                    yields.append(loop_defs[name])
            self.builder.create_yield_op([y.handle for y in yields])

        # update global uses in while_op
        for i, name in enumerate(names):
            after_block.replace_use_in_block_with(init_args[i].handle, after_block.arg(i))

        # WhileOp defines new values, update the symbol table (lscope, local_defs)
        for i, name in enumerate(names):
            new_def = triton.language.core.tensor(while_op.get_result(i), ret_types[i])
            self.lscope[name] = new_def
            self.local_defs[name] = new_def

        for stmt in node.orelse:
            assert False, "Not implemented"
            ast.NodeVisitor.generic_visit(self, stmt)

    def visit_Subscript(self, node):
        assert node.ctx.__class__.__name__ == "Load"
        lhs = self.visit(node.value)
        slices = self.visit(node.slice)
        if _is_triton_tensor(lhs):
            return lhs.__getitem__(slices, _builder=self.builder)
        return lhs[slices]

    def visit_ExtSlice(self, node):
        return [self.visit(dim) for dim in node.dims]

    def visit_For(self, node):
        IteratorClass = self.visit(node.iter.func)
        iter_args = [self.visit(arg) for arg in node.iter.args]
        if IteratorClass == triton.language.static_range:
            iterator = IteratorClass(*iter_args)
            static_range = range(iterator.start.value,
                                 iterator.end.value,
                                 iterator.step.value)
            for i in static_range:
                self.lscope[node.target.id] = triton.language.constexpr(i)
                self.visit_compound_statement(node.body)
                for stmt in node.orelse:
                    ast.NodeVisitor.generic_visit(self, stmt)
            return

        if IteratorClass is not range:
            raise RuntimeError('Only `range` and `static_range` iterators are currently supported')

        # visit iterator arguments
        # note: only `range` iterator is supported now
        # collect lower bound (lb), upper bound (ub), and step
        lb = iter_args[0] if len(iter_args) > 1 else self.visit(ast.Num(0))
        ub = iter_args[1] if len(iter_args) > 1 else self.visit(node.iter.args[0])
        step = iter_args[2] if len(iter_args) > 2 else self.visit(ast.Num(1))
        # handle negative constant step (not supported by scf.for in MLIR)
        negative_step = False
        if _is_constexpr(step) and step.value < 0:
            step = triton.language.constexpr(-step.value)
            negative_step = True
            lb, ub = ub, lb
        lb = triton.language.core._to_tensor(lb, self.builder)
        ub = triton.language.core._to_tensor(ub, self.builder)
        step = triton.language.core._to_tensor(step, self.builder)
        # induction variable type
        iv_type = triton.language.semantic.integer_promote_impl(lb.dtype, ub.dtype)
        iv_type = triton.language.semantic.integer_promote_impl(iv_type, step.dtype)
        iv_ir_type = iv_type.to_ir(self.builder)
        iv_is_signed = iv_type.int_signedness == triton.language.core.dtype.SIGNEDNESS.SIGNED
        # lb/ub/step might be constexpr, we need to cast them to tensor
        lb = lb.handle
        ub = ub.handle
        step = step.handle
        # ForOp can only accept IndexType as lb/ub/step. Cast integer to Index
        lb = self.builder.create_int_cast(lb, iv_ir_type, iv_is_signed)
        ub = self.builder.create_int_cast(ub, iv_ir_type, iv_is_signed)
        step = self.builder.create_int_cast(step, iv_ir_type, iv_is_signed)
        # Create placeholder for the loop induction variable
        iv = self.builder.create_undef(iv_ir_type)
        self.set_value(node.target.id, triton.language.core.tensor(iv, iv_type))

        with enter_sub_region(self) as sr:
            liveins, insert_block = sr
            ip = self.builder.get_insertion_point()

            # create loop body block
            block = self.builder.create_block()
            self.builder.set_insertion_point_to_start(block)
            # dry visit loop body
            self.scf_stack.append(node)
            self.visit_compound_statement(node.body)
            self.scf_stack.pop()
            block.erase()

            # If a variable (name) is defined in both its parent & itself, then it's
            # a loop-carried variable. (They must be of the same type)
            init_args = []
            yields = []
            names = []
            for name in self.local_defs:
                if name in liveins:
                    assert _is_triton_tensor(self.local_defs[name]), f'{name} is not tensor'
                    assert _is_triton_tensor(liveins[name])
                    assert self.local_defs[name].type == liveins[name].type,\
                        f'Loop-carried variable {name} has initial type {liveins[name].type} '\
                        f'but is re-assigned to {self.local_defs[name].type} in loop! '\
                        f'Please make sure that the type stays consistent.'

                    names.append(name)
                    init_args.append(triton.language.core._to_tensor(liveins[name], self.builder))
                    yields.append(triton.language.core._to_tensor(self.local_defs[name], self.builder))

            # create ForOp
            self.builder.restore_insertion_point(ip)
            for_op = self.builder.create_for_op(lb, ub, step, [arg.handle for arg in init_args])

            self.scf_stack.append(node)
            self.builder.set_insertion_point_to_start(for_op.get_body(0))
            for i, name in enumerate(names):
                self.set_value(name, triton.language.core.tensor(for_op.get_body(0).arg(i + 1), yields[i].type))
            self.visit_compound_statement(node.body)
            self.scf_stack.pop()
            yields = []
            for name in self.local_defs:
                if name in liveins:
                    yields.append(triton.language.core._to_tensor(self.local_defs[name], self.builder))

            # create YieldOp
            if len(yields) > 0:
                self.builder.create_yield_op([y.handle for y in yields])
            for_op_region = for_op.get_body(0).get_parent()
            assert for_op_region.size() == 1, "We use SCF, so the loop body should only have one block"

            # update induction variable with actual value, and replace all uses
            self.builder.set_insertion_point_to_start(for_op.get_body(0))
            iv = for_op.get_induction_var()
            if negative_step:
                iv = self.builder.create_sub(ub, iv)
                iv = self.builder.create_add(iv, lb)
            self.lscope[node.target.id].handle.replace_all_uses_with(iv)
            self.set_value(node.target.id, triton.language.core.tensor(iv, iv_type))

        # update lscope & local_defs (ForOp defines new values)
        for i, name in enumerate(names):
            self.set_value(name, triton.language.core.tensor(for_op.get_result(i), yields[i].type))

        for stmt in node.orelse:
            assert False, "Don't know what to do with else after for"
            ast.NodeVisitor.generic_visit(self, stmt)

    def visit_Slice(self, node):
        lower = self.visit(node.lower)
        upper = self.visit(node.upper)
        step = self.visit(node.step)
        return slice(lower, upper, step)

    def visit_Index(self, node):
        return self.visit(node.value)

    def visit_keyword(self, node) -> Tuple[str, Any]:
        return node.arg, self.visit(node.value)

    def visit_Assert(self, node) -> Any:
        if not self.debug:
            return
        test = self.visit(node.test)
        msg = self.visit(node.msg)
        # Convert assert to triton's device_assert which happens on the device
        return triton.language.core.device_assert(test, msg, _builder=self.builder)

    def visit_Call(self, node):
        fn = _unwrap_if_constexpr(self.visit(node.func))

        static_implementation = self.statically_implemented_functions.get(fn)
        if static_implementation is not None:
            return static_implementation(self, node)

        kws = dict(self.visit(keyword) for keyword in node.keywords)
        args = [self.visit(arg) for arg in node.args]
        if fn is triton.language.core.device_assert:   # TODO: this should not be so hardcoded
            if not self.debug:
                return
        if isinstance(fn, triton.runtime.JITFunction):
            from inspect import getcallargs
            args = getcallargs(fn.fn, *args, **kws)
            args = [args[name] for name in fn.arg_names]
            args = [arg if _is_triton_tensor(arg)
                    else triton.language.constexpr(arg) for arg in args]
            # generate function def
            attributes = dict()
            constexprs = [i for i, arg in enumerate(args) if _is_constexpr(arg)]
            constants = {i: args[i] for i in constexprs}
            # generate call
            args = [None if i in constexprs else arg for i, arg in enumerate(args)]
            arg_vals = [arg.handle for arg in args if arg is not None]
            arg_types = [arg.type for arg in args if arg is not None]
            fn_name = mangle_fn(fn.__name__, arg_types, constants)
            # generate function def if necessary
            if not self.module.has_function(fn_name):
                prototype = triton.language.function_type([], arg_types)
                gscope = sys.modules[fn.fn.__module__].__dict__
                generator = CodeGenerator(self.builder.context, prototype, gscope, attributes, constants, module=self.module, function_name=fn_name, function_types=self.function_ret_types, debug=self.debug)
                generator.visit(fn.parse())
                callee_ret_type = generator.last_ret_type
                self.function_ret_types[fn_name] = callee_ret_type
            else:
                callee_ret_type = self.function_ret_types[fn_name]
            symbol = self.module.get_function(fn_name)
            call_op = self.builder.call(symbol, arg_vals)
            if call_op.get_num_results() == 0 or callee_ret_type is None:
                return None
            elif call_op.get_num_results() == 1:
                return triton.language.tensor(call_op.get_result(0), callee_ret_type)
            else:
                # should return a tuple of tl.tensor
                results = []
                for i in range(call_op.get_num_results()):
                    results.append(triton.language.tensor(call_op.get_result(i), callee_ret_type[i]))
                return tuple(results)
        if (hasattr(fn, '__self__') and _is_triton_tensor(fn.__self__)) or impl.is_builtin(fn):
            return fn(*args, _builder=self.builder, **kws)
        if fn in self.builtin_namespace.values():
            args = map(_unwrap_if_constexpr, args)
        return fn(*args, **kws)

    def visit_Constant(self, node):
        return triton.language.constexpr(node.value)

    def visit_BoolOp(self, node: ast.BoolOp):
        if len(node.values) != 2:
            raise UnsupportedLanguageConstruct(None, node, "chained boolean operators (A or B or C) are not supported; use parentheses to split the chain.")
        lhs = self.visit(node.values[0])
        rhs = self.visit(node.values[1])
        method_name = self._method_name_for_bool_op.get(type(node.op))
        if method_name is None:
            raise UnsupportedLanguageConstruct(None, node, "AST boolean operator '{}' is not (currently) implemented.".format(node.op.__name__))
        return self._apply_binary_method(method_name, lhs, rhs)
    _method_name_for_bool_op: Dict[Type[ast.boolop], str] = {ast.And: 'logical_and', ast.Or: 'logical_or'}

    if sys.version_info < (3, 8):
        def visit_NameConstant(self, node):
            return triton.language.constexpr(node.value)

        def visit_Num(self, node):
            return triton.language.constexpr(node.n)

        def visit_Str(self, node):
            return triton.language.constexpr(ast.literal_eval(node))

    def visit_Attribute(self, node):
        lhs = self.visit(node.value)
        if _is_triton_tensor(lhs):
            if node.attr == "T":
                return triton.language.semantic.trans(lhs, builder=self.builder)
        return getattr(lhs, node.attr)

    def visit_Expr(self, node):
        ast.NodeVisitor.generic_visit(self, node)

    def visit_NoneType(self, node):
        return None

    def visit_JoinedStr(self, node):
        values = list(node.values)
        for i, value in enumerate(values):
            if isinstance(value, ast.Constant):
                values[i] = str(value.value)
            elif isinstance(value, ast.FormattedValue):
                conversion_code = value.conversion
                evaluated = self.visit(value.value)
                if not _is_constexpr(evaluated):
                    raise UnsupportedLanguageConstruct(
                        None, node, "Cannot evaluate f-string containing non-constexpr conversion values, found conversion of type " + str(type(evaluated)))
                values[i] = ("{}" if conversion_code < 0 else "{!" + chr(conversion_code) + "}").format(evaluated.value)
            else:
                raise AssertionError("encountered unexpected node of type {} in a JoinedStr node".format(type(value)))
        return ''.join(values)

    def visit(self, node):
        if node is not None:
            self.last_node = node
        with warnings.catch_warnings():
            # The ast library added visit_Constant and deprecated some other
            # methods but we can't move to that without breaking Python 3.6 and 3.7.
            warnings.simplefilter("ignore", DeprecationWarning)  # python 3.9
            warnings.simplefilter("ignore", PendingDeprecationWarning)  # python 3.8
            return super().visit(node)

    def generic_visit(self, node):
        raise UnsupportedLanguageConstruct(None, node, "unsupported AST node type: {}".format(type(node).__name__))

    # TODO: populate this here (rather than inside `_define_name_lookup`) once cyclic imports resolved
    statically_implemented_functions: Dict[object, Callable[[ast.Call], Any]] = {}

    def execute_static_print(self, node: ast.Call) -> None:
        # TODO: too simplistic? Perhaps do something else with non-constexpr

        kws = {name: _unwrap_if_constexpr(value) for name, value in (self.visit(keyword) for keyword in node.keywords)}
        args = [_unwrap_if_constexpr(self.visit(arg)) for arg in node.args]
        print(*args, **kws)

    def execute_static_assert(self, node: ast.Call) -> None:
        arg_count = len(node.args)
        if not (0 < arg_count <= 2) or len(node.keywords):
            raise TypeError("`static_assert` requires one or two positional arguments only")

        passed = self.visit(node.args[0])
        if not isinstance(passed, bool):
            raise NotImplementedError("Assertion condition could not be determined at compile-time. Make sure that it depends only on `constexpr` values")
        if not passed:
            if arg_count == 1:
                message = ""
            else:
                try:
                    message = self.visit(node.args[1])
                except Exception as e:
                    message = "<failed to evaluate assertion message: " + repr(e) + ">"

            raise CompileTimeAssertionFailure(None, node, message)
        return None


class CompilationError(Exception):
    source_line_count_max_in_message = 12

    def _format_message(self) -> str:
        node = self.node
        if self.src is None:
            source_excerpt = " <source unavailable>"
        else:
            source_excerpt = self.src.split('\n')[:node.lineno][-self.source_line_count_max_in_message:]
            if source_excerpt:
                source_excerpt.append(' ' * node.col_offset + '^')
                source_excerpt = '\n'.join(source_excerpt)
            else:
                source_excerpt = " <source empty>"

        message = "at {}:{}:{}".format(node.lineno, node.col_offset, source_excerpt)
        if self.error_message:
            message += '\n' + self.error_message
        return message

    def __init__(self, src: Optional[str], node: ast.AST, error_message: Union[str, 'triton.language.core.constexpr', None]):
        self.src = src
        self.node = node
        self.error_message = _unwrap_if_constexpr(error_message)
        self.message = self._format_message()

    def set_source_code(self, src: Optional[str]):
        self.src = src
        self.message = self._format_message()

    def __str__(self):
        return self.message

    def __repr__(self):
        return "{}({!r})".format(type(self).__name__, self.message)

    def __reduce__(self):
        # this is necessary to make CompilationError picklable
        return type(self), (self.src, self.node, self.error_message)


class CompileTimeAssertionFailure(CompilationError):
    """Specific exception for failed tests in `static_assert` invocations"""
    pass


class UnsupportedLanguageConstruct(CompilationError):
    pass


class OutOfResources(Exception):
    def __init__(self, required, limit, name):
        self.message = f'out of resource: {name}, '\
                       f'Required: {required}, '\
                       f'Hardware limit: {limit}'
        self.message += '. Reducing block sizes or `num_stages` may help.'
        self.required = required
        self.limit = limit
        self.name = name
        super().__init__(self.message)

    def __reduce__(self):
        # this is necessary to make CompilationError picklable
        return (type(self), (self.required, self.limit, self.name))


def kernel_suffix(signature, specialization):
    # suffix format:
    # <argid><'c' if equal to 1><'d' if divisible by 16>
    suffix = ''
    for i, _ in enumerate(signature):
        suffix += str(i)
        if i in specialization.equal_to_1:
            suffix += 'c'
        if i in specialization.divisible_by_16:
            suffix += 'd'
    return suffix

# ------------------------------------------------------------------------------
# ------------------------------------------------------------------------------


def parse_mlir_module(path, context):
    module = _triton.ir.parse_mlir_module(path, context)
    # module takes ownership of the context
    module.context = context
    return module


def build_triton_ir(fn, signature, specialization, constants, debug=False):
    # canonicalize signature
    if isinstance(signature, str):
        signature = {k: v.strip() for k, v in enumerate(signature.split(","))}
    context = _triton.ir.context()
    context.load_triton()
    # create kernel prototype
    cst_key = lambda i: fn.arg_names.index(i) if isinstance(i, str) else i
    constants = {cst_key(key): value for key, value in constants.items()}
    # visit kernel AST
    gscope = fn.__globals__.copy()
    function_name = '_'.join([fn.__name__, kernel_suffix(signature.values(), specialization)])
    tys = list(signature.values())
    new_constants = {k: True if k in tys and tys[k] == "i1" else 1 for k in specialization.equal_to_1}
    new_attrs = {k: ("multiple_of", 16) for k in specialization.divisible_by_16}
    all_constants = constants.copy()
    all_constants.update(new_constants)
    arg_types = [str_to_ty(v) for k, v in signature.items() if k not in constants]

    prototype = triton.language.function_type([], arg_types)
    generator = CodeGenerator(context, prototype, gscope=gscope, constants=all_constants, function_name=function_name, attributes=new_attrs, is_kernel=True, debug=debug)
    try:
        generator.visit(fn.parse())
    except CompilationError as e:
        if e.src is None:
            e.set_source_code(fn.src)
        raise
    except Exception as e:
        node = generator.last_node
        if node is None:
            raise
        raise CompilationError(fn.src, node, repr(e)) from e
    ret = generator.module
    # module takes ownership of the context
    ret.context = context
    return ret, generator


def inline_triton_ir(mod):
    pm = _triton.ir.pass_manager(mod.context)
    pm.enable_debug()
    pm.add_inliner_pass()
    pm.run(mod)
    return mod


def optimize_triton_ir(mod):
    pm = _triton.ir.pass_manager(mod.context)
    pm.enable_debug()
    pm.add_triton_combine_pass()
    pm.add_canonicalizer_pass()
    pm.add_cse_pass()
    pm.add_licm_pass()
    pm.add_symbol_dce_pass()
    pm.run(mod)
    return mod


def ttir_compute_capability_rewrite(mod, compute_capability):
    # For hardware without support, we must rewrite all load/store with block (tensor) pointers into legacy load/store
    pm = _triton.ir.pass_manager(mod.context)
    pm.enable_debug()
    pm.add_rewrite_tensor_pointer_pass(compute_capability)
    pm.run(mod)
    return mod


def ast_to_ttir(fn, signature, specialization, constants, compute_capability, debug=False):
    mod, _ = build_triton_ir(fn, signature, specialization, constants, debug)
    mod = inline_triton_ir(mod)
    mod = ttir_compute_capability_rewrite(mod, compute_capability)
    return optimize_triton_ir(mod)


def ttir_to_ttgir(mod, num_warps):
    pm = _triton.ir.pass_manager(mod.context)
    pm.enable_debug()
    pm.add_convert_triton_to_tritongpu_pass(num_warps)
    pm.run(mod)
    return mod


def optimize_ttgir(mod, num_stages, compute_capability):
    pm = _triton.ir.pass_manager(mod.context)
    pm.enable_debug()
    pm.add_tritongpu_coalesce_pass()
    pm.add_tritongpu_remove_layout_conversions_pass()
    pm.add_tritongpu_accelerate_matmul_pass(compute_capability)
    pm.add_tritongpu_remove_layout_conversions_pass()
    pm.add_tritongpu_optimize_dot_operands_pass()
    pm.add_tritongpu_pipeline_pass(num_stages)
    pm.add_tritongpu_prefetch_pass()
    pm.add_tritongpu_optimize_dot_operands_pass()
    pm.add_tritongpu_remove_layout_conversions_pass()
    pm.add_tritongpu_decompose_conversions_pass()
    pm.add_tritongpu_reorder_instructions_pass()
    pm.add_cse_pass()
    pm.add_symbol_dce_pass()
    pm.run(mod)
    return mod


def add_external_libs(mod, libs):
    for name, path in libs.items():
        if len(name) == 0 or len(path) == 0:
            return
    _triton.add_external_libs(mod, list(libs.keys()), list(libs.values()))


def ttgir_to_llir(mod, extern_libs, compute_capability):
    if extern_libs:
        add_external_libs(mod, extern_libs)
    return _triton.translate_triton_gpu_to_llvmir(mod, compute_capability)


def llir_to_ptx(mod: Any, compute_capability: int, ptx_version: int = None) -> str:
    '''
    Translate TritonGPU module to PTX code.
    :param mod: a TritonGPU dialect module
    :return: PTX code
    '''
    if ptx_version is None:
        _, cuda_version = path_to_ptxas()
        ptx_version = ptx_get_version(cuda_version)
    return _triton.translate_llvmir_to_ptx(mod, compute_capability, ptx_version)


def ptx_to_cubin(ptx: str, compute_capability: int):
    '''
    Compile TritonGPU module to cubin.
    :param ptx: ptx code
    :param compute_capability: compute capability
    :return: str
    '''
    ptxas, _ = path_to_ptxas()
    return _triton.compile_ptx_to_cubin(ptx, ptxas, compute_capability)


def ptx_get_kernel_name(ptx: str) -> str:
    '''
    Get kernel name from PTX code.
    This Kernel name is required when launching the kernel.
    '''
    # There is a name mangling in PTX codegen, so the original kernel names in Triton IR are not available in PTX/cubin.
    assert ptx
    for line in ptx.split('\n'):
        line = line.strip()
        if line.startswith('// .globl'):
            return line.split()[-1]


def amdgcn_get_kernel_name(amdgcn: str) -> str:
    '''
    Get kernel name from AMDGCN code.
    This Kernel name is required when launching the kernel.
    '''
    assert amdgcn
    for line in amdgcn.split('\n'):
        line = line.strip()
        if line.startswith('.globl'):
            return line.split()[-1].strip()


def llir_to_amdgcn_and_hsaco(mod: Any, gfx_arch: str, gfx_triple: str, gfx_features: str) -> Tuple[str, str]:
    '''
    Translate TritonGPU module to HSACO code based on full details of gpu architecture.
    :param mod: a TritonGPU dialect module
    :return:
        - AMDGCN code
        - Path to HSACO object
    '''
    return _triton.translate_llvmir_to_hsaco(mod, gfx_arch, gfx_triple, gfx_features)


@functools.lru_cache
def ptx_get_version(cuda_version) -> int:
    '''
    Get the highest PTX version supported by the current CUDA driver.
    '''
    assert isinstance(cuda_version, str)
    major, minor = map(int, cuda_version.split('.'))
    if major == 12:
        return 80 + minor
    if major == 11:
        return 70 + minor
    if major == 10:
        return 63 + minor
    raise RuntimeError("Triton only support CUDA 10.0 or higher")


def path_to_ptxas():
    base_dir = os.path.dirname(__file__)
    paths = [
        os.environ.get("TRITON_PTXAS_PATH", ""),
        os.path.join(base_dir, "third_party", "cuda", "bin", "ptxas")
    ]

    for ptxas in paths:
        if os.path.exists(ptxas) and os.path.isfile(ptxas):
            result = subprocess.check_output([ptxas, "--version"], stderr=subprocess.STDOUT)
            if result is not None:
                version = re.search(r".*release (\d+\.\d+).*", result.decode("utf-8"), flags=re.MULTILINE)
                if version is not None:
                    return ptxas, version.group(1)
    raise RuntimeError("Cannot find ptxas")


instance_descriptor = namedtuple("instance_descriptor", ["divisible_by_16", "equal_to_1"], defaults=[set(), set()])


# ------------------------------------------------------------------------------
# compiler
# ------------------------------------------------------------------------------


def ty_to_cpp(ty):
    if ty[0] == '*':
        return "hipDeviceptr_t" if torch.version.hip is not None else "CUdeviceptr"
    return {
        "i1": "int32_t",
        "i8": "int8_t",
        "i16": "int16_t",
        "i32": "int32_t",
        "i64": "int64_t",
        "u32": "uint32_t",
        "u64": "uint64_t",
        "fp16": "float",
        "bf16": "float",
        "fp32": "float",
        "f32": "float",
        "fp64": "double",
    }[ty]


def generate_name_initializer(signature):
    src = "int i = 0;\n"
    tys = signature.split(',')
    for i, ty in enumerate(tys):
        src


def binary_name_to_header_name(name):
    if len(name) > 128:
        # avoid filename too long errors (filename limit is 255)
        name = "kernel_" + hashlib.sha256(name.encode("utf-8")).hexdigest()
    return f"{name}.h"


def generate_launcher(constants, signature):
    arg_decls = ', '.join(f"{ty_to_cpp(ty)} arg{i}" for i, ty in signature.items())

    def _extracted_type(ty):
        if ty[0] == '*':
            return "PyObject*"
        return {
            'i1': 'int32_t',
            'i32': 'int32_t',
            'i64': 'int64_t',
            'u32': 'uint32_t',
            'u64': 'uint64_t',
            'fp16': 'float',
            'bf16': 'float',
            'fp32': 'float',
            'f32': 'float',
            'fp64': 'double',
        }[ty]

    def format_of(ty):
        return {
            "PyObject*": "O",
            "float": "f",
            "double": "d",
            "long": "l",
            "uint32_t": "I",
            "int32_t": "i",
            "uint64_t": "K",
            "int64_t": "L",
        }[ty]

    format = "iiiiiKKOOO" + ''.join([format_of(_extracted_type(ty)) for ty in signature.values()])

    # generate glue code
    if torch.version.hip is not None:
        src = f"""
    #define __HIP_PLATFORM_AMD__
    #include <hip/hip_runtime.h>
    #include <Python.h>
    #include <stdio.h>

    static inline void gpuAssert(hipError_t code, const char *file, int line)
    {{
      if (code != HIP_SUCCESS)
      {{
         const char* prefix = "Triton Error [HIP]: ";
         const char* str = hipGetErrorString(code);
         char err[1024] = {{0}};
         snprintf(err, 1024, "%s Code: %d, Messsage: %s", prefix, code, str );
         PyErr_SetString(PyExc_RuntimeError, err);
      }}
    }}

    #define HIP_CHECK(ans) {{ gpuAssert((ans), __FILE__, __LINE__); }}

    static void _launch(int gridX, int gridY, int gridZ, int num_warps, int shared_memory, hipStream_t stream, hipFunction_t function, {arg_decls}) {{
      void *params[] = {{ {', '.join(f"&arg{i}" for i in signature.keys() if i not in constants)} }};
      if (gridX*gridY*gridZ > 0) {{
          HIP_CHECK(hipModuleLaunchKernel(function, gridX, gridY, gridZ, 64*num_warps, 1, 1, shared_memory, stream, params, 0));
      }}
    }}

    typedef struct _DevicePtrInfo {{
      hipDeviceptr_t dev_ptr;
      bool valid;
    }} DevicePtrInfo;

    static inline DevicePtrInfo getPointer(PyObject *obj, int idx) {{
      DevicePtrInfo ptr_info;
      ptr_info.dev_ptr = 0;
      ptr_info.valid = true;

      if (PyLong_Check(obj)) {{
        ptr_info.dev_ptr = (hipDeviceptr_t)PyLong_AsUnsignedLongLong(obj);
        return ptr_info;
      }}

      if (obj == Py_None) {{
        // valid nullptr
        return ptr_info;
      }}

      PyObject *ptr = PyObject_GetAttrString(obj, "data_ptr");

      if (ptr) {{
        PyObject *empty_tuple = PyTuple_New(0);
        PyObject *ret = PyObject_Call(ptr, empty_tuple, NULL);
        Py_DECREF(empty_tuple);
        Py_DECREF(ptr);

        if (!PyLong_Check(ret)) {{
          PyErr_SetString(PyExc_TypeError, "data_ptr method of Pointer object must return 64-bit int");
          ptr_info.valid = false;
          return ptr_info;
        }}

        ptr_info.dev_ptr = (hipDeviceptr_t)PyLong_AsUnsignedLongLong(ret);

        if (!ptr_info.dev_ptr)
          return ptr_info;

        uint64_t dev_ptr;
        hipError_t status = hipPointerGetAttribute(&dev_ptr, HIP_POINTER_ATTRIBUTE_DEVICE_POINTER, ptr_info.dev_ptr);
        if (status == hipErrorInvalidValue) {{
            PyErr_Format(PyExc_ValueError,
                         "Pointer argument (at %d) cannot be accessed from Triton (cpu tensor?)", idx);
            ptr_info.valid = false;
        }}

        ptr_info.dev_ptr = (hipDeviceptr_t)dev_ptr;
        return ptr_info;
      }}

      PyErr_SetString(PyExc_TypeError, "Pointer argument must be either uint64 or have data_ptr method");
      return ptr_info;
    }}

    static PyObject* launch(PyObject* self, PyObject* args) {{

      int gridX, gridY, gridZ;
      uint64_t _stream;
      uint64_t _function;
      int num_warps;
      int shared_memory;
      PyObject *launch_enter_hook = NULL;
      PyObject *launch_exit_hook = NULL;
      PyObject *compiled_kernel = NULL;

      {' '.join([f"{_extracted_type(ty)} _arg{i}; " for i, ty in signature.items()])}
      if (!PyArg_ParseTuple(args, \"{format}\", &gridX, &gridY, &gridZ, &num_warps, &shared_memory, &_stream, &_function, &launch_enter_hook, &launch_exit_hook, &compiled_kernel, {', '.join(f"&_arg{i}" for i, ty in signature.items())})) {{
        return NULL;
      }}

      if (launch_enter_hook != Py_None) {{
        PyObject_CallObject(launch_enter_hook, args);
      }}

      // raise exception asap
      {"; ".join([f"DevicePtrInfo ptr_info{i} = getPointer(_arg{i}, {i}); if (!ptr_info{i}.valid) return NULL;" if ty[0] == "*" else "" for i, ty in signature.items()])};
      _launch(gridX, gridY, gridZ, num_warps, shared_memory, (hipStream_t)_stream, (hipFunction_t)_function, {', '.join(f"ptr_info{i}.dev_ptr" if ty[0]=="*" else f"_arg{i}" for i, ty in signature.items())});
      if (launch_exit_hook != Py_None) {{
        PyObject_CallObject(launch_exit_hook, args);
      }}
      if (PyErr_Occurred()) {{
        return NULL;
      }}

      // return None
      Py_INCREF(Py_None);
      return Py_None;
    }}

    static PyMethodDef ModuleMethods[] = {{
      {{"launch", launch, METH_VARARGS, "Entry point for all kernels with this signature"}},
      {{NULL, NULL, 0, NULL}} // sentinel
    }};

    static struct PyModuleDef ModuleDef = {{
      PyModuleDef_HEAD_INIT,
      \"__triton_launcher\",
      NULL, //documentation
      -1, //size
      ModuleMethods
    }};

    PyMODINIT_FUNC PyInit___triton_launcher(void) {{
      PyObject *m = PyModule_Create(&ModuleDef);
      if(m == NULL) {{
        return NULL;
      }}
      PyModule_AddFunctions(m, ModuleMethods);
      return m;
    }}
    """
    else:
        src = f"""
#include \"cuda.h\"
#include <stdbool.h>
#include <Python.h>

static inline void gpuAssert(CUresult code, const char *file, int line)
{{
   if (code != CUDA_SUCCESS)
   {{
      const char* prefix = "Triton Error [CUDA]: ";
      const char* str;
      cuGetErrorString(code, &str);
      char err[1024] = {{0}};
      strcat(err, prefix);
      strcat(err, str);
      PyErr_SetString(PyExc_RuntimeError, err);
   }}
}}

#define CUDA_CHECK(ans) {{ gpuAssert((ans), __FILE__, __LINE__); }}

static void _launch(int gridX, int gridY, int gridZ, int num_warps, int shared_memory, CUstream stream, CUfunction function, {arg_decls}) {{
  void *params[] = {{ {', '.join(f"&arg{i}" for i in signature.keys() if i not in constants)} }};
  if(gridX*gridY*gridZ > 0){{
    CUDA_CHECK(cuLaunchKernel(function, gridX, gridY, gridZ, 32*num_warps, 1, 1, shared_memory, stream, params, 0));
  }}
}}

typedef struct _DevicePtrInfo {{
    CUdeviceptr dev_ptr;
    bool valid;
}} DevicePtrInfo;

static inline DevicePtrInfo getPointer(PyObject *obj, int idx) {{
  DevicePtrInfo ptr_info;
  ptr_info.dev_ptr = 0;
  ptr_info.valid = true;
  if (PyLong_Check(obj)) {{
    ptr_info.dev_ptr = PyLong_AsUnsignedLongLong(obj);
    return ptr_info;
  }}
  if (obj == Py_None) {{
    // valid nullptr
    return ptr_info;
  }}
  PyObject *ptr = PyObject_GetAttrString(obj, "data_ptr");
  if(ptr){{
    PyObject *empty_tuple = PyTuple_New(0);
    PyObject *ret = PyObject_Call(ptr, empty_tuple, NULL);
    Py_DECREF(empty_tuple);
    Py_DECREF(ptr);
    if (!PyLong_Check(ret)) {{
      PyErr_SetString(PyExc_TypeError, "data_ptr method of Pointer object must return 64-bit int");
      ptr_info.valid = false;
      return ptr_info;
    }}
    ptr_info.dev_ptr = PyLong_AsUnsignedLongLong(ret);
    if(!ptr_info.dev_ptr)
      return ptr_info;
    uint64_t dev_ptr;
    int status = cuPointerGetAttribute(&dev_ptr, CU_POINTER_ATTRIBUTE_DEVICE_POINTER, ptr_info.dev_ptr);
    if (status == CUDA_ERROR_INVALID_VALUE) {{
        PyErr_Format(PyExc_ValueError,
                     "Pointer argument (at %d) cannot be accessed from Triton (cpu tensor?)", idx);
        ptr_info.valid = false;
    }}
    ptr_info.dev_ptr = dev_ptr;
    Py_DECREF(ret);  // Thanks ChatGPT!
    return ptr_info;
  }}
  PyErr_SetString(PyExc_TypeError, "Pointer argument must be either uint64 or have data_ptr method");
  return ptr_info;
}}

static PyObject* launch(PyObject* self, PyObject* args) {{
  int gridX, gridY, gridZ;
  uint64_t _stream;
  uint64_t _function;
  int num_warps;
  int shared_memory;
  PyObject *launch_enter_hook = NULL;
  PyObject *launch_exit_hook = NULL;
  PyObject *compiled_kernel = NULL;
  {' '.join([f"{_extracted_type(ty)} _arg{i}; " for i, ty in signature.items()])}
  if(!PyArg_ParseTuple(args, \"{format}\", &gridX, &gridY, &gridZ, &num_warps, &shared_memory, &_stream, &_function, &launch_enter_hook, &launch_exit_hook, &compiled_kernel, {', '.join(f"&_arg{i}" for i, ty in signature.items())})) {{
    return NULL;
  }}

  if (launch_enter_hook != Py_None) {{
    PyObject_CallObject(launch_enter_hook, args);
  }}


  // raise exception asap
  {"; ".join([f"DevicePtrInfo ptr_info{i} = getPointer(_arg{i}, {i}); if (!ptr_info{i}.valid) return NULL;" if ty[0] == "*" else "" for i, ty in signature.items()])};
  _launch(gridX, gridY, gridZ, num_warps, shared_memory, (CUstream)_stream, (CUfunction)_function, {', '.join(f"ptr_info{i}.dev_ptr" if ty[0]=="*" else f"_arg{i}"for i, ty in signature.items())});

  if (launch_exit_hook != Py_None) {{
    PyObject_CallObject(launch_exit_hook, args);
  }}

  if(PyErr_Occurred()) {{
    return NULL;
  }}
  // return None
  Py_INCREF(Py_None);
  return Py_None;
}}

static PyMethodDef ModuleMethods[] = {{
  {{"launch", launch, METH_VARARGS, "Entry point for all kernels with this signature"}},
  {{NULL, NULL, 0, NULL}} // sentinel
}};

static struct PyModuleDef ModuleDef = {{
  PyModuleDef_HEAD_INIT,
  \"__triton_launcher\",
  NULL, //documentation
  -1, //size
  ModuleMethods
}};

PyMODINIT_FUNC PyInit___triton_launcher(void) {{
  PyObject *m = PyModule_Create(&ModuleDef);
  if(m == NULL) {{
    return NULL;
  }}
  PyModule_AddFunctions(m, ModuleMethods);
  return m;
}}
"""
    return src


def default_cache_dir():
    return os.path.join(Path.home(), ".triton", "cache")


class CacheManager:

    def __init__(self, key):
        self.key = key
        self.lock_path = None
        # create cache directory if it doesn't exist
        self.cache_dir = os.environ.get('TRITON_CACHE_DIR', default_cache_dir())
        if self.cache_dir:
            self.cache_dir = os.path.join(self.cache_dir, self.key)
            self.lock_path = os.path.join(self.cache_dir, "lock")
            os.makedirs(self.cache_dir, exist_ok=True)

    def _make_path(self, filename):
        return os.path.join(self.cache_dir, filename)

    def has_file(self, filename):
        if not self.cache_dir:
            return False
        return os.path.exists(self._make_path(filename))

    def put(self, data, filename, binary=True):
        if not self.cache_dir:
            return
        binary = isinstance(data, bytes)
        if not binary:
            data = str(data)
        assert self.lock_path is not None
        filepath = self._make_path(filename)
        with FileLock(self.lock_path):
            # use tempfile to be robust against program interruptions
            mode = "wb" if binary else "w"
            with open(filepath + ".tmp", mode) as f:
                f.write(data)
            os.rename(filepath + ".tmp", filepath)


# Utilities for generating and compiling C wrappers


@functools.lru_cache()
def libcuda_dirs():
    locs = subprocess.check_output(["whereis", "libcuda.so"]).decode().strip().split()[1:]
    return [os.path.dirname(loc) for loc in locs]


@contextlib.contextmanager
def quiet():
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        yield
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr


@functools.lru_cache()
def rocm_path_dir():
    return os.getenv("ROCM_PATH", default="/opt/rocm")


def _build(name, src, srcdir):
    if torch.version.hip is not None:
        hip_lib_dir = os.path.join(rocm_path_dir(), "lib")
        hip_include_dir = os.path.join(rocm_path_dir(), "include")
    else:
        cuda_lib_dirs = libcuda_dirs()
        base_dir = os.path.dirname(__file__)
        cuda_path = os.path.join(base_dir, "third_party", "cuda")

        cu_include_dir = os.path.join(cuda_path, "include")
        triton_include_dir = os.path.join(os.path.dirname(__file__), "include")
        cuda_header = os.path.join(cu_include_dir, "cuda.h")
        triton_cuda_header = os.path.join(triton_include_dir, "cuda.h")
        if not os.path.exists(cuda_header) and os.path.exists(triton_cuda_header):
            cu_include_dir = triton_include_dir
    suffix = sysconfig.get_config_var('EXT_SUFFIX')
    so = os.path.join(srcdir, '{name}{suffix}'.format(name=name, suffix=suffix))
    # try to avoid setuptools if possible
    cc = os.environ.get("CC")
    if cc is None:
        # TODO: support more things here.
        clang = shutil.which("clang")
        gcc = shutil.which("gcc")
        cc = gcc if gcc is not None else clang
        if cc is None:
            raise RuntimeError("Failed to find C compiler. Please specify via CC environment variable.")
    # This function was renamed and made public in Python 3.10
    if hasattr(sysconfig, 'get_default_scheme'):
        scheme = sysconfig.get_default_scheme()
    else:
        scheme = sysconfig._get_default_scheme()
    # 'posix_local' is a custom scheme on Debian. However, starting Python 3.10, the default install
    # path changes to include 'local'. This change is required to use triton with system-wide python.
    if scheme == 'posix_local':
        scheme = 'posix_prefix'
    py_include_dir = sysconfig.get_paths(scheme=scheme)["include"]

    if torch.version.hip is not None:
        ret = subprocess.check_call([cc, src, f"-I{hip_include_dir}", f"-I{py_include_dir}", f"-I{srcdir}", "-shared", "-fPIC", f"-L{hip_lib_dir}", "-lamdhip64", "-o", so])
    else:
        cc_cmd = [cc, src, "-O3", f"-I{cu_include_dir}", f"-I{py_include_dir}", f"-I{srcdir}", "-shared", "-fPIC", "-lcuda", "-o", so]
        cc_cmd += [f"-L{dir}" for dir in cuda_lib_dirs]
        ret = subprocess.check_call(cc_cmd)

    if ret == 0:
        return so
    # fallback on setuptools
    extra_compile_args = []
    library_dirs = cuda_lib_dirs
    include_dirs = [srcdir, cu_include_dir]
    libraries = ['cuda']
    # extra arguments
    extra_link_args = []
    # create extension module
    ext = setuptools.Extension(
        name=name,
        language='c',
        sources=[src],
        include_dirs=include_dirs,
        extra_compile_args=extra_compile_args + ['-O3'],
        extra_link_args=extra_link_args,
        library_dirs=library_dirs,
        libraries=libraries,
    )
    # build extension module
    args = ['build_ext']
    args.append('--build-temp=' + srcdir)
    args.append('--build-lib=' + srcdir)
    args.append('-q')
    args = dict(
        name=name,
        ext_modules=[ext],
        script_args=args,
    )
    with quiet():
        setuptools.setup(**args)
    return so


def make_so_cache_key(version_hash, signature, constants):
    # Get unique key for the compiled code
    signature = {k: 'ptr' if v[0] == '*' else v for k, v in signature.items()}
    key = f"{version_hash}-{''.join(signature.values())}{constants}"
    key = hashlib.md5(key.encode("utf-8")).hexdigest()
    return key


def make_fn_cache_key(fn_hash, signature, configs, constants, num_warps, num_stages):
    # Get unique key for the compiled code
    get_conf_key = lambda conf: (sorted(conf.divisible_by_16), sorted(conf.equal_to_1))
    configs_key = [get_conf_key(conf) for conf in configs]
    key = f"{fn_hash}-{''.join(signature.values())}-{configs_key}-{constants}-{num_warps}-{num_stages}"
    key = hashlib.md5(key.encode("utf-8")).hexdigest()
    return key


def read_or_execute(cache_manager, force_compile, file_name, metadata,
                    run_if_found: Callable[[str], bytes] = None,
                    run_if_not_found: Callable = None):
    suffix = file_name.split(".")[1]
    if not force_compile and cache_manager.has_file(file_name):
        module = run_if_found(cache_manager._make_path(file_name))
        data = module if isinstance(module, bytes) else str(module).encode("utf-8")
        md5 = hashlib.md5(data).hexdigest()
        has_changed = metadata and md5 != metadata["md5"][suffix]
        return module, md5, has_changed, True
    module = run_if_not_found()
    data = module if isinstance(module, bytes) else str(module).encode("utf-8")
    md5 = hashlib.md5(data).hexdigest()
    cache_manager.put(data, file_name, True if isinstance(data, bytes) else data)
    return module, md5, True, False


def get_amdgpu_arch_fulldetails():
    """
    get the amdgpu fulll ISA details for compiling:
    i.e., arch_triple: amdgcn-amd-amdhsa; arch_name: gfx906; arch_features: sramecc+:xnack-
    """
    try:
        rocminfo = subprocess.check_output(rocm_path_dir() + '/bin/rocminfo').decode()
        gfx_arch_details = re.search('amd.*', rocminfo).group(0).strip().split('--')
        arch_triple = gfx_arch_details[0]
        arch_name_features = gfx_arch_details[1].split(':')
        arch_name = arch_name_features[0]
        arch_features = ""

        if (len(arch_name_features) == 3):
            arch_features = "+" + re.search('\\w+', arch_name_features[1]).group(0) + ","\
                            "-" + re.search('\\w+', arch_name_features[2]).group(0)
        return [arch_triple, arch_name, arch_features]
    except BaseException:
        return None


def make_stub(name, signature, constants):
    # name of files that are cached
    so_cache_key = make_so_cache_key(triton.runtime.jit.version_key(), signature, constants)
    so_cache_manager = CacheManager(so_cache_key)
    so_name = f"{name}.so"
    # retrieve stub from cache if it exists
    if not so_cache_manager.has_file(so_name):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = generate_launcher(constants, signature)
            src_path = os.path.join(tmpdir, "main.c")
            with open(src_path, "w") as f:
                f.write(src)
            so = _build(name, src_path, tmpdir)
            with open(so, "rb") as f:
                so_cache_manager.put(f.read(), so_name, binary=True)
    return so_cache_manager._make_path(so_name)


def convert_type_repr(x):
    match = re.search(r'!tt\.ptr<(.*)>', x)
    if match is not None:
        return '*' + convert_type_repr(match.group(1))
    return x


def make_hash(fn, **kwargs):
    if isinstance(fn, triton.runtime.JITFunction):
        configs = kwargs["configs"]
        signature = kwargs["signature"]
        constants = kwargs.get("constants", dict())
        num_warps = kwargs.get("num_warps", 4)
        num_stages = kwargs.get("num_stages", 3)
        # Get unique key for the compiled code
        get_conf_key = lambda conf: (sorted(conf.divisible_by_16), sorted(conf.equal_to_1))
        configs_key = [get_conf_key(conf) for conf in configs]
        key = f"{fn.cache_key}-{''.join(signature.values())}-{configs_key}-{constants}-{num_warps}-{num_stages}"
        return hashlib.md5(key.encode("utf-8")).hexdigest()
    assert isinstance(fn, str)
    return hashlib.md5((Path(fn).read_text() + triton.runtime.jit.version_key()).encode("utf-8")).hexdigest()


# - ^\s*func\.func\s+ : match the start of the string, any leading whitespace, the keyword func,
#    and any following whitespace
# - (public\s+)? : optionally match the keyword public and any following whitespace
# - (@\w+) : match an @ symbol followed by one or more word characters
#   (letters, digits, or underscores), and capture it as group 1 (the function name)
# - (\((?:%\w+: \S+(?: \{\S+ = \S+ : \S+\})?(?:, )?)*\)) : match a pair of parentheses enclosing
#   zero or more arguments separated by commas, and capture it as group 2 (the argument list)
mlir_prototype_pattern = r'^\s*func\.func\s+(?:public\s+)?(@\w+)(\((?:%\w+: \S+(?: \{\S+ = \S+ : \S+\})?(?:, )?)*\))\s*\{\s*$'
ptx_prototype_pattern = r"\.(?:visible|extern)\s+\.(?:entry|func)\s+(\w+)\s*\(([^)]*)\)"
prototype_pattern = {
    "ttir": mlir_prototype_pattern,
    "ttgir": mlir_prototype_pattern,
    "ptx": ptx_prototype_pattern,
}

mlir_arg_type_pattern = r'%\w+: ([^,^\)\s]+)(?: \{\S+ = \S+ : \S+\})?,?'
ptx_arg_type_pattern = r"\.param\s+\.(\w+)"
arg_type_pattern = {
    "ttir": mlir_arg_type_pattern,
    "ttgir": mlir_arg_type_pattern,
    "ptx": ptx_arg_type_pattern,
}


def _get_jsonable_constants(constants):
    def _is_jsonable(x):
        try:
            json.dumps(x)
            return True
        except (TypeError, OverflowError):
            return False
    serialized_constants = {}
    for constant in constants:
        if _is_jsonable(constants[constant]):
            serialized_constants[constant] = constants[constant]
    return serialized_constants

# def compile(fn, signature: str, device: int = -1, constants=dict(), num_warps: int = 4, num_stages: int = 3, extern_libs=None, configs=None):


@static_vars(discovered_gfx_arch_fulldetails=get_amdgpu_arch_fulldetails())
def compile(fn, **kwargs):
    capability = kwargs.get("cc", None)
    if capability is None:
        device = triton.runtime.jit.get_current_device()
        capability = triton.runtime.jit.get_device_capability(device)
        capability = capability[0] * 10 + capability[1]
    # we get the kernel, i.e. the first function generated in the module
    # if fn is not a JITFunction, then it
    # has to be a path to a file
    context = _triton.ir.context()
    asm = dict()
    constants = kwargs.get("constants", dict())
    num_warps = kwargs.get("num_warps", 4)
    num_stages = kwargs.get("num_stages", 3 if capability >= 75 else 2)
    extern_libs = kwargs.get("extern_libs", dict())
    debug = kwargs.get("debug", False)
    # build compilation stages
    if torch.version.hip is not None:
        _triton.set_rocm()
        if extern_libs is None:
            extern_libs = get_amdgcn_bitcode_paths()
        else:
            extern_libs.update(get_amdgcn_bitcode_paths())

        for key in list(extern_libs):
            if extern_libs[key] == '' or extern_libs[key] is None:
                extern_libs.pop(key)

        gfx_arch_full_details = compile.discovered_gfx_arch_fulldetails
        gfx_arch = os.environ.get('MI_GPU_ARCH', gfx_arch_full_details[1])
        if gfx_arch is None:
            raise RuntimeError('gfx_arch is None (not specified)')
        stages = {
            "ast": (lambda path: fn, None),
            "ttir": (lambda path: parse_mlir_module(path, context),
                     lambda src: ast_to_ttir(src, signature, configs[0], constants, capability)),
            "ttgir": (lambda path: parse_mlir_module(path, context),
                      lambda src: optimize_ttgir(ttir_to_ttgir(src, num_warps), num_stages, capability)),
            "llir": (lambda path: Path(path).read_text(),
                     lambda src: ttgir_to_llir(src, extern_libs, capability)),
            "amdgcn": (lambda path: Path(path).read_text(),
                       lambda src: llir_to_amdgcn_and_hsaco(src, gfx_arch,
                                                            gfx_arch_full_details[0],
                                                            gfx_arch_full_details[2])),
        }
    else:
        stages = {
            "ast": (lambda path: fn, None),
            "ttir": (lambda path: parse_mlir_module(path, context),
                     lambda src: ast_to_ttir(src, signature, configs[0], constants, capability, debug)),
            "ttgir": (lambda path: parse_mlir_module(path, context),
                      lambda src: optimize_ttgir(ttir_to_ttgir(src, num_warps), num_stages, capability)),
            "llir": (lambda path: Path(path).read_text(),
                     lambda src: ttgir_to_llir(src, extern_libs, capability)),
            "ptx": (lambda path: Path(path).read_text(),
                    lambda src: llir_to_ptx(src, capability)),
            "cubin": (lambda path: Path(path).read_bytes(),
                      lambda src: ptx_to_cubin(src, capability))
        }
    # find out the signature of the function
    if isinstance(fn, triton.runtime.JITFunction):
        configs = kwargs.get("configs", None)
        signature = kwargs["signature"]
        if configs is None:
            configs = [instance_descriptor()]
        assert len(configs) == 1
        kwargs["configs"] = configs
        name = fn.__name__
        first_stage = 0
        if isinstance(signature, str):
            signature = {k: v.strip() for k, v in enumerate(signature.split(","))}
        kwargs["signature"] = signature
    else:
        assert isinstance(fn, str)
        _, ir = os.path.basename(fn).split(".")
        src = Path(fn).read_text()
        import re
        match = re.search(prototype_pattern[ir], src, re.MULTILINE)
        name, signature = match.group(1), match.group(2)
        # print(name, signature)
        types = re.findall(arg_type_pattern[ir], signature)
        # print(types)
        param_tys = [convert_type_repr(ty) for ty in types]
        signature = {k: v for k, v in enumerate(param_tys)}
        first_stage = list(stages.keys()).index(ir)

    # cache manager
    so_path = make_stub(name, signature, constants)
    # create cache manager
    fn_cache_manager = CacheManager(make_hash(fn, **kwargs))
    # determine name and extension type of provided function
    if isinstance(fn, triton.runtime.JITFunction):
        name, ext = fn.__name__, "ast"
    else:
        name, ext = os.path.basename(fn).split(".")

    # load metadata if any
    metadata = None
    if fn_cache_manager.has_file(f'{name}.json'):
        with open(fn_cache_manager._make_path(f"{name}.json")) as f:
            metadata = json.load(f)
    else:
        metadata = {"num_warps": num_warps, "num_stages": num_stages,
                    "constants": _get_jsonable_constants(constants), "ctime": dict(), "debug": debug}
        if ext == "ptx":
            assert "shared" in kwargs, "ptx compilation must provide shared memory size"
            metadata["shared"] = kwargs["shared"]

    first_stage = list(stages.keys()).index(ext)
    asm = dict()
    module = fn
    # run compilation pipeline  and populate metadata
    for ir, (parse, compile_kernel) in list(stages.items())[first_stage:]:
        path = fn_cache_manager._make_path(f"{name}.{ir}")
        if ir == ext:
            next_module = parse(fn)
        elif os.path.exists(path) and\
                ir in metadata["ctime"] and\
                os.path.getctime(path) == metadata["ctime"][ir]:
            if ir == "amdgcn":
                next_module = (parse(path), parse(fn_cache_manager._make_path(f"{name}.hsaco_path")))
            else:
                next_module = parse(path)
        else:
            next_module = compile_kernel(module)
            if ir == "amdgcn":
                fn_cache_manager.put(next_module[0], f"{name}.{ir}")
                fn_cache_manager.put(next_module[1], f"{name}.hsaco_path")
            else:
                fn_cache_manager.put(next_module, f"{name}.{ir}")
        if os.path.exists(path):
            metadata["ctime"][ir] = os.path.getctime(path)
        if ir == "cubin":
            asm[ir] = next_module
        elif ir == "amdgcn":
            asm[ir] = str(next_module[0])
        else:
            asm[ir] = str(next_module)
        if ir == "llir" and "shared" not in metadata:
            metadata["shared"] = _triton.get_shared_memory_size(module)
        if ir == "ptx":
            metadata["name"] = ptx_get_kernel_name(next_module)
        if ir == "amdgcn":
            metadata["name"] = amdgcn_get_kernel_name(next_module[0])
            asm["hsaco_path"] = next_module[1]
        module = next_module
    # write-back metadata
    fn_cache_manager.put(json.dumps(metadata), f"{name}.json", binary=False)
    # return handle to compiled kernel
    return CompiledKernel(fn, so_path, metadata, asm)


@static_vars(discovered_gfx_arch_fulldetails=get_amdgpu_arch_fulldetails())
def _get_amdgcn_bitcode_paths():
    if torch.version.hip is not None:
        gpu_arch_agnostic_bitcode_libraries = ["opencl.bc",
                                               "ocml.bc",
                                               "ockl.bc",
                                               "oclc_finite_only_off.bc",
                                               "oclc_daz_opt_off.bc",
                                               "oclc_correctly_rounded_sqrt_on.bc",
                                               "oclc_unsafe_math_off.bc",
                                               "oclc_wavefrontsize64_on.bc"]

        gfx_arch = _get_amdgcn_bitcode_paths.discovered_gfx_arch_fulldetails[1]
        gfx_arch_id = re.search('gfx(\\w+)', gfx_arch).group(1).strip()

        gpu_arch_specific_bitcode_library = 'oclc_isa_version_' + gfx_arch_id + ".bc"
        bitcode_path_dir = os.path.join(Path(__file__).parent.resolve(), "third_party/rocm/lib/bitcode/")

        amdgcn_bitcode_paths = {}
        i = 1
        for bc_lib in gpu_arch_agnostic_bitcode_libraries:
            bc_path = bitcode_path_dir + bc_lib
            if os.path.exists(bc_path):
                amdgcn_bitcode_paths['library_' + str(i)] = bc_path
                i += 1
        bc_gfx_path = bitcode_path_dir + gpu_arch_specific_bitcode_library
        if os.path.exists(bc_gfx_path):
            amdgcn_bitcode_paths['library_' + str(i)] = bc_gfx_path

        return amdgcn_bitcode_paths
    else:
        return {}


@static_vars(amdgcn_bitcode_paths=_get_amdgcn_bitcode_paths())
def get_amdgcn_bitcode_paths():
    return get_amdgcn_bitcode_paths.amdgcn_bitcode_paths


class CompiledKernel:

    # Hooks for external tools to monitor the execution of triton kernels
    launch_enter_hook = None
    launch_exit_hook = None

    def __init__(self, fn, so_path, metadata, asm):
        # initialize launcher
        import importlib.util
        spec = importlib.util.spec_from_file_location("__triton_launcher", so_path)
        mod = importlib.util.module_from_spec(spec)
        self.fn = fn
        spec.loader.exec_module(mod)
        self.c_wrapper = getattr(mod, "launch")
        # initialize metadata
        self.shared = metadata["shared"]
        self.num_warps = metadata["num_warps"]
        self.num_stages = metadata["num_stages"]
        self.constants = metadata["constants"]
        # initialize asm dict
        self.asm = asm
        # binaries are lazily initialized
        # because it involves doing runtime things
        # (e.g., checking amount of shared memory on current device)
        self.metadata = metadata
        self.cu_module = None
        self.cu_function = None

    def _init_handles(self):
        if self.cu_module is not None:
            return
        device = triton.runtime.jit.get_current_device()
        if torch.version.hip is not None:
            global hip_utils
            init_hip_utils()
            max_shared = hip_utils.get_device_properties(device)["max_shared_mem"]
            if self.shared > max_shared:
                raise OutOfResources(self.shared, max_shared, "shared memory")
            mod, func, n_regs, n_spills = hip_utils.load_binary(self.metadata["name"], self.asm["hsaco_path"], self.shared, device)
        else:
            global cuda_utils
            init_cuda_utils()
            max_shared = cuda_utils.get_device_properties(device)["max_shared_mem"]
            if self.shared > max_shared:
                raise OutOfResources(self.shared, max_shared, "shared memory")
            mod, func, n_regs, n_spills = cuda_utils.load_binary(self.metadata["name"], self.asm["cubin"], self.shared, device)

        self.n_spills = n_spills
        self.n_regs = n_regs
        self.cu_module = mod
        self.cu_function = func

    def __getattribute__(self, name):
        if name == 'c_wrapper':
            self._init_handles()
        return super().__getattribute__(name)

    def __getitem__(self, grid):
        self._init_handles()

        def runner(*args, stream=None):
            if stream is None:
                stream = triton.runtime.jit.get_cuda_stream()
            self.c_wrapper(grid[0], grid[1], grid[2], self.num_warps, self.shared, stream, self.cu_function,
                           CompiledKernel.launch_enter_hook, CompiledKernel.launch_exit_hook, self, *args)
        return runner

    def get_sass(self, fun=None):
        if 'sass' in self.asm:
            return self.asm['sass']
        fd, path = tempfile.mkstemp()
        try:
            with open(fd, 'wb') as cubin:
                cubin.write(self.asm['cubin'])
            self.sass = extract(path, fun)
        finally:
            os.remove(path)
        self.asm['sass'] = self.sass
        return self.sass


class CudaUtils(object):

    def __new__(cls):
        if not hasattr(cls, 'instance'):
            cls.instance = super(CudaUtils, cls).__new__(cls)
        return cls.instance

    @staticmethod
    def _generate_src():
        return """
        #include <cuda.h>

        #include \"cuda.h\"
        #define PY_SSIZE_T_CLEAN
        #include <Python.h>

        static inline void gpuAssert(CUresult code, const char *file, int line)
        {
           if (code != CUDA_SUCCESS)
           {
              const char* prefix = "Triton Error [CUDA]: ";
              const char* str;
              cuGetErrorString(code, &str);
              char err[1024] = {0};
              strcat(err, prefix);
              strcat(err, str);
              PyErr_SetString(PyExc_RuntimeError, err);
           }
        }

        #define CUDA_CHECK(ans) { gpuAssert((ans), __FILE__, __LINE__); if(PyErr_Occurred()) return NULL; }

        static PyObject* getDeviceProperties(PyObject* self, PyObject* args){
            int device_id;
            if(!PyArg_ParseTuple(args, "i", &device_id))
                return NULL;
            // Get device handle
            CUdevice device;
            cuDeviceGet(&device, device_id);

            // create a struct to hold device properties
            int max_shared_mem;
            int multiprocessor_count;
            int sm_clock_rate;
            int mem_clock_rate;
            int mem_bus_width;
            CUDA_CHECK(cuDeviceGetAttribute(&max_shared_mem, CU_DEVICE_ATTRIBUTE_MAX_SHARED_MEMORY_PER_BLOCK_OPTIN, device));
            CUDA_CHECK(cuDeviceGetAttribute(&multiprocessor_count, CU_DEVICE_ATTRIBUTE_MULTIPROCESSOR_COUNT, device));
            CUDA_CHECK(cuDeviceGetAttribute(&sm_clock_rate, CU_DEVICE_ATTRIBUTE_CLOCK_RATE, device));
            CUDA_CHECK(cuDeviceGetAttribute(&mem_clock_rate, CU_DEVICE_ATTRIBUTE_MEMORY_CLOCK_RATE, device));
            CUDA_CHECK(cuDeviceGetAttribute(&mem_bus_width, CU_DEVICE_ATTRIBUTE_GLOBAL_MEMORY_BUS_WIDTH, device));


            return Py_BuildValue("{s:i, s:i, s:i, s:i, s:i}", "max_shared_mem", max_shared_mem,
                                       "multiprocessor_count", multiprocessor_count,
                                       "sm_clock_rate", sm_clock_rate,
                                       "mem_clock_rate", mem_clock_rate,
                                       "mem_bus_width", mem_bus_width);
        }

        static PyObject* loadBinary(PyObject* self, PyObject* args) {
            const char* name;
            const char* data;
            Py_ssize_t data_size;
            int shared;
            int device;
            if(!PyArg_ParseTuple(args, "ss#ii", &name, &data, &data_size, &shared, &device)) {
                return NULL;
            }
            CUfunction fun;
            CUmodule mod;
            int32_t n_regs = 0;
            int32_t n_spills = 0;
            // create driver handles
            CUDA_CHECK(cuModuleLoadData(&mod, data));
            CUDA_CHECK(cuModuleGetFunction(&fun, mod, name));
            // get allocated registers and spilled registers from the function
            CUDA_CHECK(cuFuncGetAttribute(&n_regs, CU_FUNC_ATTRIBUTE_NUM_REGS, fun));
            CUDA_CHECK(cuFuncGetAttribute(&n_spills, CU_FUNC_ATTRIBUTE_LOCAL_SIZE_BYTES, fun));
            n_spills /= 4;
            // set dynamic shared memory if necessary
            int shared_optin;
            CUDA_CHECK(cuDeviceGetAttribute(&shared_optin, CU_DEVICE_ATTRIBUTE_MAX_SHARED_MEMORY_PER_BLOCK_OPTIN, device));
            if (shared > 49152 && shared_optin > 49152) {
              CUDA_CHECK(cuFuncSetCacheConfig(fun, CU_FUNC_CACHE_PREFER_SHARED));
              int shared_total, shared_static;
              CUDA_CHECK(cuDeviceGetAttribute(&shared_total, CU_DEVICE_ATTRIBUTE_MAX_SHARED_MEMORY_PER_MULTIPROCESSOR, device));
              CUDA_CHECK(cuFuncGetAttribute(&shared_static, CU_FUNC_ATTRIBUTE_SHARED_SIZE_BYTES, fun));
              CUDA_CHECK(cuFuncSetAttribute(fun, CU_FUNC_ATTRIBUTE_MAX_DYNAMIC_SHARED_SIZE_BYTES, shared_optin - shared_static));
            }

            if(PyErr_Occurred()) {
              return NULL;
            }
            return Py_BuildValue("(KKii)", (uint64_t)mod, (uint64_t)fun, n_regs, n_spills);
        }

        static PyMethodDef ModuleMethods[] = {
          {"load_binary", loadBinary, METH_VARARGS, "Load provided cubin into CUDA driver"},
          {"get_device_properties", getDeviceProperties, METH_VARARGS, "Get the properties for a given device"},
          {NULL, NULL, 0, NULL} // sentinel
        };

        static struct PyModuleDef ModuleDef = {
          PyModuleDef_HEAD_INIT,
          \"cuda_utils\",
          NULL, //documentation
          -1, //size
          ModuleMethods
        };

        PyMODINIT_FUNC PyInit_cuda_utils(void) {
          PyObject *m = PyModule_Create(&ModuleDef);
          if(m == NULL) {
            return NULL;
          }
          PyModule_AddFunctions(m, ModuleMethods);
          return m;
        }
        """

    def __init__(self):
        src = self._generate_src()
        key = hashlib.md5(src.encode("utf-8")).hexdigest()
        cache = CacheManager(key)
        fname = "cuda_utils.so"
        if not cache.has_file(fname):
            with tempfile.TemporaryDirectory() as tmpdir:
                src_path = os.path.join(tmpdir, "main.c")
                with open(src_path, "w") as f:
                    f.write(src)
                so = _build("cuda_utils", src_path, tmpdir)
                with open(so, "rb") as f:
                    cache.put(f.read(), fname, binary=True)
        import importlib.util
        spec = importlib.util.spec_from_file_location("cuda_utils", cache._make_path(fname))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.load_binary = mod.load_binary
        self.get_device_properties = mod.get_device_properties


def init_cuda_utils():
    global cuda_utils
    if cuda_utils is None:
        cuda_utils = CudaUtils()


cuda_utils = None


def init_hip_utils():
    global hip_utils
    if hip_utils is None:
        hip_utils = HIPUtils()


hip_utils = None


class HIPUtils(object):
    def __new__(cls):
        if not hasattr(cls, 'instance'):
            cls.instance = super(HIPUtils, cls).__new__(cls)
        return cls.instance

    def _generate_src(self):
        return """
        #define __HIP_PLATFORM_AMD__
        #include <hip/hip_runtime.h>
        #define PY_SSIZE_T_CLEAN
        #include <Python.h>
        #include <stdio.h>
        #include <stdlib.h>
        static inline void gpuAssert(hipError_t code, const char *file, int line)
        {{
          if (code != HIP_SUCCESS)
          {{
             const char* prefix = "Triton Error [HIP]: ";
             const char* str = hipGetErrorString(code);
             char err[1024] = {0};
             snprintf(err, 1024, "%s Code: %d, Messsage: %s", prefix, code, str );
             PyErr_SetString(PyExc_RuntimeError, err);
          }}
        }}

        #define HIP_CHECK(ans) { gpuAssert((ans), __FILE__, __LINE__); if (PyErr_Occurred()) return NULL; }

        static PyObject* getDeviceProperties(PyObject* self, PyObject* args){
            int device_id;
            if (!PyArg_ParseTuple(args, "i", &device_id))
                return NULL;

            hipDeviceProp_t props;
            HIP_CHECK(hipGetDeviceProperties(&props, device_id));

            // create a struct to hold device properties
            return Py_BuildValue("{s:i, s:i, s:i, s:i, s:i}", "max_shared_mem", props.sharedMemPerBlock,
                                       "multiprocessor_count", props.multiProcessorCount,
                                       "sm_clock_rate", props.clockRate,
                                       "mem_clock_rate", props.memoryClockRate,
                                       "mem_bus_width", props.memoryBusWidth);
        }

        static PyObject* loadBinary(PyObject* self, PyObject* args) {
            const char* name;
            const char* data;
            Py_ssize_t data_size;
            int shared;
            int device;
            if (!PyArg_ParseTuple(args, "ss#ii", &name, &data, &data_size, &shared, &device)) {
                return NULL;
            }

            // Open HSACO file
            FILE* hsaco_file;
            if ((hsaco_file = fopen(data, "rb")) == NULL) {
                return NULL;
            }

            // Read HSCAO file into Buffer
            fseek(hsaco_file, 0L, SEEK_END);
            size_t hsaco_file_size = ftell(hsaco_file);
            unsigned char* hsaco = (unsigned char*) malloc(hsaco_file_size * sizeof(unsigned char));
            rewind(hsaco_file);
            fread(hsaco, sizeof(unsigned char), hsaco_file_size, hsaco_file);
            fclose(hsaco_file);

            // set HIP options
            hipJitOption opt[] = {hipJitOptionErrorLogBufferSizeBytes, hipJitOptionErrorLogBuffer,
                                  hipJitOptionInfoLogBufferSizeBytes, hipJitOptionInfoLogBuffer,
                                  hipJitOptionLogVerbose};
            const unsigned int errbufsize = 8192;
            const unsigned int logbufsize = 8192;
            char _err[errbufsize];
            char _log[logbufsize];
            void *optval[] = {(void *)(uintptr_t)errbufsize,
                              (void *)_err, (void *)(uintptr_t)logbufsize,
                              (void *)_log, (void *)1};

            // launch HIP Binary
            hipModule_t mod;
            hipFunction_t fun;
            hipModuleLoadDataEx(&mod, hsaco, 5, opt, optval);
            hipModuleGetFunction(&fun, mod, name);
            free(hsaco);

            // get allocated registers and spilled registers from the function
            int n_regs = 0;
            int n_spills = 0;
            if (PyErr_Occurred()) {
              return NULL;
            }
            return Py_BuildValue("(KKii)", (uint64_t)mod, (uint64_t)fun, n_regs, n_spills);
        }

        static PyMethodDef ModuleMethods[] = {
          {"load_binary", loadBinary, METH_VARARGS, "Load provided hsaco into HIP driver"},
          {"get_device_properties", getDeviceProperties, METH_VARARGS, "Get the properties for a given device"},
          {NULL, NULL, 0, NULL} // sentinel
        };

        static struct PyModuleDef ModuleDef = {
          PyModuleDef_HEAD_INIT,
          "hip_utils",
          NULL, //documentation
          -1, //size
          ModuleMethods
        };

        PyMODINIT_FUNC PyInit_hip_utils(void) {
          PyObject *m = PyModule_Create(&ModuleDef);
          if (m == NULL) {
            return NULL;
          }
          PyModule_AddFunctions(m, ModuleMethods);
          return m;
        }
        """

    def __init__(self):
        src = self._generate_src()
        key = hashlib.md5(src.encode("utf-8")).hexdigest()
        cache = CacheManager(key)
        fname = "hip_utils.so"
        if not cache.has_file(fname):
            with tempfile.TemporaryDirectory() as tmpdir:
                src_path = os.path.join(tmpdir, "main.c")
                with open(src_path, "w") as f:
                    f.write(src)
                so = _build("hip_utils", src_path, tmpdir)
                with open(so, "rb") as f:
                    cache.put(f.read(), fname, binary=True)
        import importlib.util
        spec = importlib.util.spec_from_file_location("hip_utils", cache._make_path(fname))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.load_binary = mod.load_binary
        self.get_device_properties = mod.get_device_properties
