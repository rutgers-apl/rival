'''
Translate expressions into SMT via Z3
'''

from language import *
from typing import TypeConstraints
from z3util import *
import config
import z3, operator, logging


logger = logging.getLogger(__name__)

def is_const(term):
  return isinstance(term, Constant) or \
    (isinstance(term, Input) and term.name[0] == 'C')

def _mk_bop(op, defined = None, poisons = None):
  def bop(self, term):
    x = self.eval(term.x)
    y = self.eval(term.y)

    if defined:
      self.add_defs(*defined(x,y))

    if poisons:
      for f in term.flags:
        self.add_nops(poisons[f](x,y))
  
    return op(x,y)
  
  return bop

def _mk_fp_bop(op):
  def bop(self, term):
    x = self.eval(term.x)
    y = self.eval(term.y)

    if 'nnan' in term.flags:
      self.add_defs(z3.Not(z3.fpIsNaN(x)), z3.Not(z3.fpIsNaN(y)), 
        z3.Not(z3.fpIsNaN(op(x,y))))

    if 'ninf' in term.flags:
      self.add_defs(z3.Not(z3.fpIsInfinite(x)), z3.Not(z3.fpIsInfinite(y)),
        z3.Not(z3.fpIsInfinite(op(x,y))))

    return op(x,y)

  return bop

def _mk_must_analysis(op):
  def pred(self, term):
    x = self.eval(term._args[0])

    if is_const(x):
      return op(x)

    c = self.fresh_bool()
    self.add_defs(z3.Implies(c, op(x)))
    return c

  return pred

def _mk_bin_must_analysis(op):
  def bop(self, term):
    x = self.eval(term._args[0])
    y = self.eval(term._args[1])

    if all(is_const(a) for a in term._args):
      return op(x,y)

    c = self.fresh_bool()
    self.add_defs(z3.Implies(c, op(x,y)))
    return c

  return bop

def _ty_sort(ty):
  'Translate a Type expression to a Z3 Sort'

  if isinstance(ty, IntType):
    return z3.BitVecSort(ty.width)

  return {
    PtrType: z3.BitVecSort(64),
    HalfType: z3.FloatHalf(),
    SingleType: z3.Float32(),
    DoubleType: z3.Float64()}[type(ty)]
    # NOTE: this assumes the global z3 context never changes

class SMTTranslator(Visitor):
  log = logger.getChild('SMTTranslator')

  def __init__(self, type_model):
    self.types = type_model
    self.fresh = 0
    self.defs = []  # current defined-ness conditions
    self.nops = []  # current non-poison conditions
    self.qvars = []

  def eval(self, term):
    '''smt.eval(term) -> Z3 expression

    Translate the term (and subterms), adding its definedness conditons,
    nonpoison conditions, and quantifier variables to the state.
    '''
    self.log.debug('eval %s', term)
    return term.accept(self)

  def __call__(self, term):
    '''smt(term) -> Z3 expression, def conds, nonpoison conds, qvars

    Clear the current state, translate the term (and subterms), and
    return the translation, definedness conditions, nonpoison conditions,
    and quantified variables.

    Quantified variables are guaranteed to be unique between different
    calls to the same SMTTranslator object.
    '''
    self.log.debug('call %s', term)
    self.defs = []
    self.nops = []
    self.qvars = []
    v = term.accept(self)
    return v, self.defs, self.nops, self.qvars

  def add_defs(self, *defs):
    self.defs += defs

  def add_nops(self, *nops):
    self.nops += nops
  
  def add_qvar(self, *qvars):
    self.qvars += qvars

  def bits(self, term):
    ty = self.types[term]
    if isinstance(ty, IntType):
      return ty.width
    if isinstance(ty, PtrType):
      return 64
      # NOTE: assume 64-bit pointers, since we don't do anything with them

    assert False

  def fresh_bool(self):
    self.fresh += 1
    return z3.Bool('ana_' + str(self.fresh))

  def fresh_bv(self, size, prefix='ana_'):
    self.fresh += 1
    return z3.BitVec(prefix + str(self.fresh), size)

  def Input(self, term):
    # TODO: unique name check

    ty = self.types[term]
    return z3.Const(term.name, _ty_sort(ty))

  AddInst = _mk_bop(operator.add,
    poisons =
      {'nsw': lambda x,y: z3.SignExt(1,x)+z3.SignExt(1,y) == z3.SignExt(1,x+y),
       'nuw': lambda x,y: z3.ZeroExt(1,x)+z3.ZeroExt(1,y) == z3.ZeroExt(1,x+y)})

  SubInst = _mk_bop(operator.sub,
    poisons =
      {'nsw': lambda x,y: z3.SignExt(1,x)-z3.SignExt(1,y) == z3.SignExt(1,x-y),
       'nuw': lambda x,y: z3.ZeroExt(1,x)-z3.ZeroExt(1,y) == z3.ZeroExt(1,x-y)})

  MulInst = _mk_bop(operator.mul,
    poisons =
      {'nsw': lambda x,y: z3.SignExt(x.size(),x)*z3.SignExt(x.size(),y) == z3.SignExt(x.size(),x*y),
       'nuw': lambda x,y: z3.ZeroExt(x.size(),x)*z3.ZeroExt(x.size(),y) == z3.ZeroExt(x.size(),x*y)})

  SDivInst = _mk_bop(operator.div,
    defined = lambda x,y: [y != 0, z3.Or(x != (1 << x.size()-1), y != -1)],
    poisons = {'exact': lambda x,y: (x/y)*y == x})

  UDivInst = _mk_bop(z3.UDiv,
    defined = lambda x,y: [y != 0],
    poisons = {'exact': lambda x,y: z3.UDiv(x,y)*y == x})

  SRemInst = _mk_bop(z3.SRem,
    defined = lambda x,y: [y != 0, z3.Or(x != (1 << (x.size()-1)), y != -1)])

  URemInst = _mk_bop(z3.URem,
    defined = lambda x,y: [y != 0])
  
  ShlInst = _mk_bop(operator.lshift,
    defined = lambda x,y: [z3.ULT(y, y.size())],
    poisons =
      {'nsw': lambda x,y: (x << y) >> y == x,
       'nuw': lambda x,y: z3.LShR(x << y, y) == x})

  AShrInst = _mk_bop(operator.rshift,
    defined = lambda x,y: [z3.ULT(y, y.size())],
    poisons = {'exact': lambda x,y: (x >> y) << y == x})

  LShrInst = _mk_bop(z3.LShR,
    defined = lambda x,y: [z3.ULT(y, y.size())],
    poisons = {'exact': lambda x,y: z3.LShR(x, y) << y == x})

  AndInst = _mk_bop(operator.and_)
  OrInst = _mk_bop(operator.or_)
  XorInst = _mk_bop(operator.xor)

  FAddInst = _mk_fp_bop(operator.add)
  FSubInst = _mk_fp_bop(operator.sub)
  FMulInst = _mk_fp_bop(operator.mul)
  FDivInst = _mk_fp_bop(lambda x,y: z3.fpDiv(z3._dflt_rm(), x, y))
  FRemInst = _mk_fp_bop(z3.fpRem)


  def SExtInst(self, term):
    v = self.eval(term.arg)
    src = self.bits(term.arg)
    tgt = self.bits(term)
    return z3.SignExt(tgt - src, v)

  def ZExtInst(self, term):
    v = self.eval(term.arg)
    src = self.bits(term.arg)
    tgt = self.bits(term)
    return z3.ZeroExt(tgt - src, v)

  def TruncInst(self, term):
    v = self.eval(term.arg)
    tgt = self.bits(term)
    return z3.Extract(tgt - 1, 0, v)

  def ZExtOrTruncInst(self, term):
    v = self.eval(term.arg)
    src = self.bits(term.arg)
    tgt = self.bits(term)
    
    if tgt == src:
      return v
    if tgt > src:
      return z3.ZeroExt(tgt - src, v)
    
    return z3.Extract(tgt-1, 0, v)

  def IcmpInst(self, term):
    x = self.eval(term.x)
    y = self.eval(term.y)

    cmp = {
      'eq': operator.eq,
      'ne': operator.ne,
      'ugt': z3.UGT,
      'uge': z3.UGE,
      'ult': z3.ULT,
      'ule': z3.ULE,
      'sgt': operator.gt,
      'sge': operator.ge,
      'slt': operator.lt,
      'sle': operator.le}[term.pred](x,y)

    return bool_to_BitVec(cmp)
  
  def SelectInst(self, term):
    c = self.eval(term.sel)
    x = self.eval(term.arg1)
    y = self.eval(term.arg2)
    
    return z3.If(c == 1, x, y)

  def Literal(self, term):
    return z3.BitVecVal(term.val, self.bits(term))

  def FLiteral(self, term):
    ty = self.types[term]
    assert isinstance(ty, FloatType)

    return z3.FPVal(term.val, _ty_sort(ty))


  def UndefValue(self, term):
    ty = self.types[term]
    self.fresh += 1
    name = 'undef_' + str(self.fresh)

    x = z3.Const(name, _ty_sort(ty))
    self.add_qvar(x)
    return x

  # NOTE: constant expressions do no introduce poison or definedness constraints
  #       is this reasonable?
  AddCnxp = _mk_bop(operator.add)
  SubCnxp = _mk_bop(operator.sub)
  MulCnxp = _mk_bop(operator.mul)
  SDivCnxp = _mk_bop(operator.div)
  UDivCnxp = _mk_bop(z3.UDiv)
  SRemCnxp = _mk_bop(z3.SRem)
  URemCnxp = _mk_bop(z3.URem)
  ShlCnxp = _mk_bop(operator.lshift)
  AShrCnxp = _mk_bop(operator.rshift)
  LShrCnxp = _mk_bop(z3.LShR)
  AndCnxp = _mk_bop(operator.and_)
  OrCnxp = _mk_bop(operator.or_)
  XorCnxp = _mk_bop(operator.xor)

  def NotCnxp(self, term):
    return ~self.eval(term.x)

  def NegCnxp(self, term):
    return -self.eval(term.x)

  def AbsCnxp(self, term):
    x = self.eval(term._args[0])

    return z3.If(x >= 0, x, -x)

  def SignBitsCnxp(self, term):
    x = self.eval(term._args[0])
    size = self.bits(term)

    #b = ComputeNumSignBits(self.fresh_bv(size), size)
    b = self.fresh_bv(size)
    
    self.add_defs(z3.ULE(b, ComputeNumSignBits(x, size)))

    return b

  def OneBitsCnxp(self, term):
    x = self.eval(term._args[0])
    b = self.fresh_bv(x.size())
    
    self.add_defs(b & ~x == 0)

    return b

  def ZeroBitsCnxp(self, term):
    x = self.eval(term._args[0])
    b = self.fresh_bv(x.size())
    
    self.add_defs(b & x == 0)

    return b

  def LeadingZerosCnxp(self, term):
    x = self.eval(term._args[0])

    return ctlz(x, self.bits(term))

  def TrailingZerosCnxp(self, term):
    x = self.eval(term._args[0])
    
    return cttz(x, self.bits(term))

  def Log2Cnxp(self, term):
    x = self.eval(term._args[0])

    return bv_log2(x, self.bits(term))

  def LShrFunCnxp(self, term):
    x = self.eval(term._args[0])
    y = self.eval(term._args[1])

    return z3.LShR(x,y)

  def SMaxCnxp(self, term):
    x = self.eval(term._args[0])
    y = self.eval(term._args[1])

    return z3.If(x > y, x, y)

  def UMaxCnxp(self, term):
    x = self.eval(term._args[0])
    y = self.eval(term._args[1])

    return z3.If(z3.UGT(x,y), x, y)

  def SExtCnxp(self, term):
    x = self.eval(term._args[0])

    bits = self.bits(term)
    return z3.SignExt(bits - x.size(), x)

  def ZExtCnxp(self, term):
    x = self.eval(term._args[0])

    bits = self.bits(term)
    return z3.ZeroExt(bits - x.size(), x)

  def TruncCnxp(self, term):
    x = self.eval(term._args[0])

    bits = self.bits(term)
    return z3.Extract(bits-1, 0, x)

  def WidthCnxp(self, term):
    return z3.BitVecVal(self.bits(term._args[0]), self.bits(term))
    # NOTE: nothing bad should happen if we don't evaluate the argument

  def AndPred(self, term):
    return z3.And([self.eval(cl) for cl in term.clauses])

  def OrPred(self, term):
    return z3.Or([self.eval(cl) for cl in term.clauses])

  def NotPred(self, term):
    return z3.Not(self.eval(term.p))

  def Comparison(self, term):
    cmp = {
      'eq': operator.eq,
      'ne': operator.ne,
      'ugt': z3.UGT,
      'uge': z3.UGE,
      'ult': z3.ULT,
      'ule': z3.ULE,
      'sgt': operator.gt,
      'sge': operator.ge,
      'slt': operator.lt,
      'sle': operator.le}[term.op]

    return cmp(self.eval(term.x), self.eval(term.y))

  def IntMinPred(self, term):
    x = self.eval(term._args[0])

    return x == 1 << (x.size()-1)

  Power2Pred = _mk_must_analysis(lambda x: z3.And(x != 0, x & (x-1) == 0))
  Power2OrZPred = _mk_must_analysis(lambda x: x & (x-1) == 0)

  def ShiftedMaskPred(self, term):
    x = self.eval(term._args[0])

    v = (x - 1) | x
    return z3.And(v != 0, ((v+1) & v) == 0)

  MaskZeroPred = _mk_bin_must_analysis(lambda x,y: x & y == 0)

  NSWAddPred = _mk_bin_must_analysis(
    lambda x,y: z3.SignExt(1,x) + z3.SignExt(1,y) == z3.SignExt(1,x+y))

  NUWAddPred = _mk_bin_must_analysis(
    lambda x,y: z3.ZeroExt(1,x) + z3.ZeroExt(1,y) == z3.ZeroExt(1,x+y))

  NSWSubPred = _mk_bin_must_analysis(
    lambda x,y: z3.SignExt(1,x) - z3.SignExt(1,y) == z3.SignExt(1,x-y))

  NUWSubPred = _mk_bin_must_analysis(
    lambda x,y: z3.ZeroExt(1,x) - z3.ZeroExt(1,y) == z3.ZeroExt(1,x-y))

  def NSWMulPred(self, term):
    x = self.eval(term._args[0])
    y = self.eval(term._args[1])

    size = x.size()
    return z3.SignExt(size,x) * z3.SignExt(size,y) == z3.SignExt(size,x*y)

  def NUWMulPred(self, term):
    x = self.eval(term._args[0])
    y = self.eval(term._args[1])

    size = x.size()
    return z3.ZeroExt(size,x) * z3.ZeroExt(size,y) == z3.ZeroExt(size,x*y)

  def NUWShlPred(self, term):
    x = self.eval(term._args[0])
    y = self.eval(term._args[1])

    return z3.LShR(x << y, y) == x

  def OneUsePred(self, term):
    return z3.BoolVal(True)
    # NOTE: should this have semantics?

#FIXME: this belongs somewhere higher in the heirarchy
def format_ty(ty):
  if isinstance(ty, IntType):
    return 'i' + str(ty.width)

  return {
    PtrType: 'pointer',
    HalfType: 'half',
    SingleType: 'float',
    DoubleType: 'double'}[type(ty)]

def format_z3val(val):
  #if isinstance(ty, (IntType, PtrType)):
  if isinstance(val, z3.BitVecNumRef):
    w = val.size()
    u = val.as_long()
    s = val.as_signed_long()

    if u == s:
      return '0x{1:0{0}X} ({1})'.format((w+3)/4, u)
    return '0x{1:0{0}X} ({1}, {2})'.format((w+3)/4, u, s)

  #if isinstance(ty, FloatType):
  if isinstance(val, z3.FPRef):
    return str(val) #val.as_string()

class RefinementError(object): # exception?
  UB, POISON, UNEQUAL = range(3)

  def __init__(self, cause, model, types, src, srcv, tgtv, ids):
    self.cause = cause
    self.model = model
    self.types = types
    self.src   = src
    self.srcv  = srcv
    self.tgtv  = tgtv
    self.ids   = ids

  cause_str = {
    UB:      'Target introduces undefined behavior',
    POISON:  'Target introduces poison',
    UNEQUAL: 'Mismatch in values',
    }

  def write(self):
    print 'ERROR:', self.cause_str[self.cause],
    print 'for', format_ty(self.types[self.src]), self.src.name

    for k,v in self.ids:
      print k.name, '=', format_ty(self.types[k]),
      print format_z3val(self.model.evaluate(v, True))

    src_v = self.model.evaluate(self.srcv, True)
    print
    print 'source:', format_z3val(src_v)

    if self.cause == self.UB:
      print 'target: undefined'
    elif self.cause == self.POISON:
      print 'target: poison'
    else:
      tgt_v = self.model.evaluate(self.tgtv, True)
      print 'target:', format_z3val(tgt_v)

def _get_inputs(term):
  for t in subterms(term):
    if isinstance(t, Input):
      yield t

def check_refinement_at(type_model, src, tgt, pre=None):
  smt = SMTTranslator(type_model)
  
  sv,sd,sp,qvars = smt(src)

  tv,td,tp,_ = smt(tgt)
  if pre:
    pb,pd,_,_ = smt(pre)
    sd += [pb] + pd
    # NOTE: should we require sd => pd?


  
  if config.poison_undef:
    expr = sd + sp + [z3.Not(z3.And(td))]
  else:
    expr = sd + [z3.Not(z3.And(td))]
  
  if qvars:
    expr = z3.ForAll(qvars, z3.And(expr))

  s = z3.Solver()
  s.add(expr)
  logger.debug('undef check\n%s', s)
  if s.check() != z3.unsat:
    m = s.model()
    logger.debug('counterexample: %s', m)
    ids = [(i, smt.eval(i)) for i in _get_inputs(src)]
    return RefinementError(RefinementError.UB,
      m, type_model, src, sv, tv, ids)
  
  expr = sd + sp + [z3.Not(z3.And(tp))]
  if qvars:
    expr = z3.ForAll(qvars, z3.And(expr))

  s = z3.Solver()
  s.add(expr)
  logger.debug('poison check\n%s', s)
  if s.check() != z3.unsat:
    m = s.model()
    logger.debug('counterexample: %s', m)
    ids = [(i, smt.eval(i)) for i in _get_inputs(src)]
    return RefinementError(RefinementError.POISON,
      s.model(), type_model, src, sv, tv, ids)
  
  if isinstance(type_model[src], FloatType):
    expr = sd + sp + [sv != tv, z3.Not(z3.And(z3.fpIsNaN(sv), z3.fpIsNaN(tv)))]
  else:
    expr = sd + sp + [sv != tv]

  if qvars:
    expr = z3.ForAll(qvars, z3.And(expr))
  
  s = z3.Solver()
  s.add(expr)
  logger.debug('equality check\n%s', s)
  if s.check() != z3.unsat:
    m = s.model()
    logger.debug('counterexample: %s', m)
    ids = [(i, smt.eval(i)) for i in _get_inputs(src)]
    return RefinementError(RefinementError.UNEQUAL,
      s.model(), type_model, src, sv, tv, ids)

  return None


def check_refinement(e1, e2, pre=None):
  T = TypeConstraints()
  T.eq_types(e1,e2)
  if pre:
    pre.accept(T)
  
  for m in T.z3_models():
    logger.debug('using model %s', m)
    r = check_refinement_at(m, e1, e2, pre)
    if r:
      return r


  return None
  

def interp(e):
  T = TypeConstraints()
  T(e)
  
  m = T.z3_models().next()
  
  smt = SMTTranslator(m)
  return smt.eval(e)


if __name__ == '__main__':
  logging.basicConfig() #level=logging.DEBUG)

  r = check_refinement(IcmpInst('ult', Input('x'), Literal(0)), Literal(0))
  print r if r else 'Okay'

# Pre: C1 < C2
# %Op0 = ashr exact %X, C1
# %r = shl i33 %Op0, C2
#   =>
# %r = shl %X, C2-C1

  C1 = Input('C1')
  C2 = Input('C2')
  x = Input('%X')

  op0 = AShrInst(x, C1, flags=['exact'])
  rs = ShlInst(op0, C2)
  rt = ShlInst(x, SubCnxp(C2,C1))
  pre = Comparison('slt',C1,C2)

  r = check_refinement(rs,rt,pre)
  print r if r else 'Okay'

  pre = IntMinPred(C1)
  src = AddInst(XorInst(x,C1),C2)
  tgt = AddInst(x, XorCnxp(C1,C2))
  r = check_refinement(src,tgt,pre)
  print r if r else 'Okay'
