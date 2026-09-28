"""Solver layer for the plant LP, on the open-source HiGHS solver.

The model is written against a small modelling interface (Model, addVar, addVars, addConstr,
setObjective, quicksum, LinExpr, GRB constants, Var.X). This module implements it as a thin
pure-Python layer over HiGHS (pip install highspy): variables and constraints are collected as
sparse linear expressions, assembled into one column-wise matrix and passed to HiGHS in a
single call.

Solver options are set with Model.setParam(name, value) using HiGHS option names (output_flag,
solver, threads, time_limit, ...); main.py holds the campaign defaults in SOLVER_PARAMS.

The model is a pure LP (continuous variables, linear constraints, linear objective), which is
all this layer supports; it raises on anything else rather than approximating it.
"""
from __future__ import annotations
import logging
import time

import numpy as np
from scipy.sparse import csr_matrix
import highspy

log = logging.getLogger(__name__)
BACKEND = 'highs'

_INF = float('inf')

class GRB:                                   # the constants the model refers to
    INFINITY = _INF
    MINIMIZE, MAXIMIZE = 1, -1
    OPTIMAL, INFEASIBLE, INF_OR_UNBD, UNBOUNDED, TIME_LIMIT = 2, 3, 4, 5, 9

class _Lin:
    """Shared arithmetic of variables and linear expressions."""
    __array_ufunc__ = None                   # numpy scalars defer to __rmul__/__radd__
    __hash__ = object.__hash__

    def __add__(self, o):  return LinExpr(self).__iadd__(o)
    __radd__ = __add__
    def __sub__(self, o):  return LinExpr(self).__isub__(o)
    def __rsub__(self, o): return (-LinExpr(self)).__iadd__(o)
    def __neg__(self):     return LinExpr(self).__imul__(-1.0)
    def __mul__(self, c):  return LinExpr(self).__imul__(c)
    __rmul__ = __mul__
    def __truediv__(self, c): return LinExpr(self).__imul__(1.0 / float(c))
    def __le__(self, o): return _Constr(LinExpr(self).__isub__(o), '<')
    def __ge__(self, o): return _Constr(LinExpr(self).__isub__(o), '>')
    def __eq__(self, o): return _Constr(LinExpr(self).__isub__(o), '=')

class Var(_Lin):
    __slots__ = ('index', 'name', 'X')
    def __init__(self, index, name):
        self.index, self.name, self.X = index, name, None

class LinExpr(_Lin):
    """sum(coef * var) + const, stored as {column index: coefficient}."""
    __slots__ = ('terms', 'const')
    def __init__(self, arg=0.0):
        if isinstance(arg, Var):
            self.terms, self.const = {arg.index: 1.0}, 0.0
        elif isinstance(arg, LinExpr):
            self.terms, self.const = dict(arg.terms), arg.const
        else:
            self.terms, self.const = {}, float(arg)

    def __iadd__(self, o, sign=1.0):
        if isinstance(o, Var):
            self.terms[o.index] = self.terms.get(o.index, 0.0) + sign
        elif isinstance(o, LinExpr):
            t = self.terms
            for j, c in o.terms.items():
                t[j] = t.get(j, 0.0) + sign * c
            self.const += sign * o.const
        else:
            self.const += sign * float(o)
        return self
    def __isub__(self, o): return self.__iadd__(o, -1.0)
    def __imul__(self, c):
        if isinstance(c, _Lin):
            raise TypeError('The HiGHS backend supports linear models only (variable x variable found).')
        c = float(c)
        for j in self.terms:
            self.terms[j] *= c
        self.const *= c
        return self

    def getValue(self, x=None):
        return self.const + sum(c * _cur_x[j] for j, c in self.terms.items())

_cur_x = None

class _Constr:
    __slots__ = ('expr', 'sense')
    def __init__(self, expr, sense): self.expr, self.sense = expr, sense
    def __bool__(self):
        raise TypeError('A constraint was used as a truth value; pass it to Model.addConstr().')

def quicksum(items):
    out = LinExpr()
    for it in items:
        out += it
    return out

class Model:
    def __init__(self, name=''):
        self.name, self._vars, self._lb, self._ub = name, [], [], []
        self._rows, self._obj, self._sense = [], LinExpr(), GRB.MINIMIZE
        self._params, self.Status, self.ObjVal, self.Runtime = {}, None, None, None

    # ---- building ----
    def addVar(self, lb=0.0, ub=_INF, obj=0.0, vtype=None, name=''):
        if vtype not in (None, 'C'):
            raise TypeError('The HiGHS backend supports continuous variables only.')
        v = Var(len(self._vars), name)
        self._vars.append(v); self._lb.append(float(lb)); self._ub.append(float(ub))
        return v

    def addVars(self, n, lb=0.0, ub=_INF, name=''):
        return {i: self.addVar(lb=lb, ub=ub, name=f'{name}[{i}]') for i in range(n)}

    def addConstr(self, c, name=''):
        if not isinstance(c, _Constr):
            raise TypeError(f'addConstr expects a linear comparison, got {type(c).__name__}')
        self._rows.append(c)
        return c

    def setObjective(self, expr, sense=1):
        self._obj, self._sense = LinExpr(expr), sense

    def setParam(self, key, value): self._params[key] = value
    def update(self): pass

    # ---- solving ----
    def optimize(self):
        global _cur_x
        n, rows = len(self._vars), self._rows
        indptr, indices, data = [0], [], []
        rlo, rup = np.empty(len(rows)), np.empty(len(rows))
        for i, c in enumerate(rows):
            e = c.expr
            indices.extend(e.terms.keys()); data.extend(e.terms.values()); indptr.append(len(indices))
            rhs = -e.const
            rlo[i] = -_INF if c.sense == '<' else rhs
            rup[i] = _INF if c.sense == '>' else rhs
        A = csr_matrix((data, indices, indptr), shape=(len(rows), n)).tocsc()
        A.eliminate_zeros()

        lp = highspy.HighsLp()
        lp.num_col_, lp.num_row_ = n, len(rows)
        cost = np.zeros(n)
        for j, cf in self._obj.terms.items():
            cost[j] = cf
        lp.col_cost_, lp.offset_ = cost, self._obj.const
        lp.col_lower_, lp.col_upper_ = np.array(self._lb), np.array(self._ub)
        lp.row_lower_, lp.row_upper_ = rlo, rup
        lp.sense_ = highspy.ObjSense.kMinimize if self._sense == GRB.MINIMIZE else highspy.ObjSense.kMaximize
        lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
        lp.a_matrix_.start_, lp.a_matrix_.index_, lp.a_matrix_.value_ = A.indptr, A.indices, A.data

        h = highspy.Highs()
        for k, v in self._params.items():                # HiGHS option names: output_flag, solver, threads, time_limit, ...
            if h.setOptionValue(k, v) != highspy.HighsStatus.kOk:
                log.warning('HiGHS did not accept option %s=%r', k, v)
        h.passModel(lp)
        t0 = time.time(); h.run(); self.Runtime = time.time() - t0

        ms = h.getModelStatus()
        self.Status = {highspy.HighsModelStatus.kOptimal: GRB.OPTIMAL,
                       highspy.HighsModelStatus.kInfeasible: GRB.INFEASIBLE,
                       highspy.HighsModelStatus.kUnboundedOrInfeasible: GRB.INF_OR_UNBD,
                       highspy.HighsModelStatus.kUnbounded: GRB.UNBOUNDED,
                       highspy.HighsModelStatus.kTimeLimit: GRB.TIME_LIMIT}.get(ms, -1)
        if self.Status == GRB.OPTIMAL:
            x = np.asarray(h.getSolution().col_value)
            _cur_x = x
            for v in self._vars:
                v.X = float(x[v.index])
            self.ObjVal = h.getInfo().objective_function_value
        else:
            log.warning('HiGHS finished with status %s', h.modelStatusToString(ms))

    @property
    def NumVars(self): return len(self._vars)
    @property
    def NumConstrs(self): return len(self._rows)
