'''
Apply typing constraints to the IR.
'''

from . import config
from .language import *
from .util import disjoint, pretty
import logging, itertools
import collections
import weakref

logger = logging.getLogger(__name__)

FIRST_CLASS, NUMBER, FLOAT, INT_PTR, PTR, INT, BOOL, Last = range(8)

context = weakref.WeakKeyDictionary()
  # Maps nodes to type ids
  # Uses weak refs, to enable creating many typed terms without having to worry
  # about garbage collection (useful when enumerating possible preconditions)
  #
  # Presently, nodes are stored by their hash (ie, by pointer). This means:
  # 1. the same node cannot have two different type ids, so sharing nodes between
  #    transformations is dangerous
  # 2. any switch to structural equality for nodes will require changing how this
  #    works (eg, log2(C) may have different types in different contexts, presently
  #    they are different objects)
  #
  # It is possible to avoid using a global here by including context in the
  # TypeModel, but this would possibly complicate things elsewhere.

# Use this type for comparisons and predicates with ambiguously-typed arguments
predicate_default = IntType(64)


def most_specific(c1,c2):
  if c1 > c2:
    c1,c2 = c2,c1

  if c1 == NUMBER:
    if c2 == PTR:
      return None

    if c2 == INT_PTR:
      return INT

  if c1 == FLOAT and c2 != FLOAT:
    return None

  if c1 == PTR and c2 != PTR:
    return None

  return c2


_constraint_name = {
  FIRST_CLASS: 'first class',
  NUMBER:      'integer or floating-point',
  FLOAT:       'floating-point',
  INT_PTR:     'integer or pointer',
  PTR:         'pointer',
  INT:         'integer',
  BOOL:        'i1',
}

_constraint_class = {
  FIRST_CLASS: (IntType, FloatType, PtrType),
  NUMBER:      (IntType, FloatType),
  FLOAT:       FloatType,
  INT_PTR:     (IntType, PtrType),
  PTR:         PtrType,
  INT:         IntType,
}

def meets_constraint(con, ty):
  if con == BOOL:
    return ty == IntType(1)

  return isinstance(ty, _constraint_class[con])



class TypeConstraints(object):
  logger = logger.getChild('TypeConstraints')
  def __init__(self, maxwidth=64):
    self.sets = disjoint.DisjointSubsets()
    self.specifics = {}
    self.constraints = collections.defaultdict(lambda: FIRST_CLASS)
    self.ordering = set() # pairs (x,y) where width(x) < width(y)
    self.width_equalities = set() # pairs (x,y) where width(x) == width(y)
    self.widthlimit = maxwidth+1
    self.default_rep = None

  def collect(self, term, seen = None):
    """Gather type constraints for this term and its subterms.

    If seen is provided, terms in seen will not be gathered.
    """
    for t in subterms(term, seen):
      t.type_constraints(self)

  def rep(self, term):
    """Return the representative member of the unification set containing this
    term. Creates and initializes a unification set if one did not previously
    exist.
    """
    try:
      return self.sets.rep(term)

    except KeyError:
      assert isinstance(term, Value)
      self._init_term(term)
      return term

  def _init_term(self, term):
    self.logger.debug('adding term %s', term)
    self.sets.add_key(term)

  def _merge(self, t1, t2):
    self.logger.debug('unifying %s and %s', t1, t2)

    if t2 in self.specifics:
      self.specific(t1, self.specifics.pop(t2))

    if t2 in self.constraints:
      self.constrain(t1, self.constraints.pop(t2))

    if t2 is self.default_rep:
      self.default_rep = t1


  def eq_types(self, *terms):
    it = iter(terms)
    t1 = self.rep(next(it))
    for t2 in it:
      self.sets.unify(t1, self.rep(t2), self._merge)

  def _init_default(self, rep):
    self.specific(rep, predicate_default)
    self.constrain(rep, INT)
    self.default_rep = rep

  def default(self, term):
    if self.default_rep is None:
      self._init_default(self.rep(term))
    else:
      self.eq_types(term, self.default_rep)

  def specific(self, term, ty):
    r = self.rep(term)

    if ty is None:
      return

    self.logger.debug('specifying %s : %s', term, ty)
    if r not in self.specifics:
      self.specifics[r] = ty
    if self.specifics[r] != ty:
      raise Error('Incompatible types for {}: {} and {}'.format(
        term.name if hasattr(term, 'name') else str(term),
        ty,
        self.specifics[term]))

  def constrain(self, term, con):
    r = self.rep(term)
    con0 = self.constraints[r]

    self.logger.debug('Refining constraint for %s: %s & %s', term, con, con0)
    c = most_specific(con0, con)
    if c is None:
      raise Error('Incompatible constraints for {}: {} and {}'.format(
        term.name if hasattr(term, 'name') else str(term),
        _constraint_name[con],
        _constraint_name[con0]))

    self.constraints[r] = c

  def integer(self, term):
    self.constrain(term, INT)

  def bool(self, term):
    self.constrain(term, BOOL)

  def pointer(self, term):
    self.constrain(term, PTR)

  def int_ptr_vec(self, term):
    self.constrain(term, INT_PTR)

  def float(self, term):
    self.constrain(term, FLOAT)

  def number(self, term):
    self.constrain(term, NUMBER)

  def first_class(self, term):
    self.constrain(term, FIRST_CLASS)

  def width_order(self, lo, hi):
    if isinstance(lo, Value):
      lo = self.rep(lo)
    hi = self.rep(hi)
    self.ordering.add((lo,hi))

  def width_equal(self, a, b):
    a = self.rep(a)
    b = self.rep(b)
    self.width_equalities.add((a,b))

  def validate(self):
    '''Make sure specific types meet constraints'''

    for r in self.specifics:
      if r not in self.constraints:
        continue

      if not meets_constraint(self.constraints[r], self.specifics[r]):
        raise Error('Incompatible constraints for {}: {} is not {}'.format(
          r.name if hasattr(term, 'name') else str(r),
          self.specifics[r],
          _constraint_name[self.constraints[r]]))

  def simplify_orderings(self):
    if self.logger.isEnabledFor(logging.DEBUG):
      self.logger.debug('simplifying ordering:\n  ' +
        pretty.pformat(self.ordering, indent=2) +
        '\n  equalities:\n' + pretty.pformat(self.width_equalities, indent=2))

    ords = { (lo if isinstance(lo, int) else self.sets.rep(lo), self.sets.rep(hi))
              for (lo,hi) in self.ordering }

    eqs = { (self.sets.rep(a), self.sets.rep(b))
      for (a,b) in self.width_equalities if a != b }
    eqs = { (a,b) if id(a) < id(b) else (b,a) for (a,b) in eqs}

    if self.logger.isEnabledFor(logging.DEBUG):
      self.logger.debug('simplified ordering:\n  ' +
        pretty.pformat(ords, indent=2) +
        '\n  equalities:\n' + pretty.pformat(eqs, indent=2))

    assert all(isinstance(lo, int) or
      most_specific(self.constraints[lo], self.constraints[hi]) is not None
      for (lo, hi) in ords)

    self.ordering = ords
    self.width_equalities = eqs

  def get_type_model(self):
    """Return an AbstractTypeModel expressing the constraints gathered so far,
    and sets the type variable for each term.
    """

    self.simplify_orderings()
    # TODO: this can be folded into the next loop

    # find predecessors and lower bounds
    lower_bounds = collections.defaultdict(list)
    min_width = {}
    for lo,hi in self.ordering:
      if isinstance(lo, int):
        min_width[hi] = max(lo, min_width.get(hi,0))
      else:
        lower_bounds[hi].append(lo)

    if logger.isEnabledFor(logging.DEBUG):
      logger.debug('get_type_model:\n  min_width: ' +
        pretty.pformat(min_width, indent=13) +
        '\n  lower_bounds: ' + pretty.pformat(lower_bounds, indent=16))

    # recursively walk DAG
    # TODO: handle all specific constraints first?
    finished = {}
    order = []
    def visit(rep):
      if rep in finished:
        if finished[rep]:
          return

        # if rep is in finished, but we haven't set it to true, then
        # we must have found a loop
        raise Error('Incompatible constraints for {}: circular ordering'.format(
          rep.name if hasattr(rep, 'name') else str(rep)
          ))

      finished[rep] = False
      for p in lower_bounds[rep]:
        visit(p)

      order.append(rep)
      finished[rep] = True

    for r in self.sets.reps():
      visit(r)

    tyvars = dict(itertools.izip(order, itertools.count()))
    if logger.isEnabledFor(logging.DEBUG):
      logger.debug('get_type_model:\n  tyvars: ' +
        pretty.pformat(tyvars, indent=10))

    min_width = { tyvars[rep]: w for (rep,w) in min_width.iteritems() }
    lower_bounds = { tyvars[rep]: tuple(tyvars[t] for t in ts)
                      for (rep,ts) in lower_bounds.iteritems() if ts }

    # recreate specific and constraint in terms of tyvars
    specific = {}
    constraint = []
    for tyvar,rep in enumerate(order):
      if rep in self.specifics:
        specific[tyvar] = self.specifics[rep]
        if not meets_constraint(self.constraints[rep], self.specifics[rep]):
          raise Error('Incompatible constraints for {}: {} is not {}'.format(
            rep.name if hasattr(rep, 'name') else str(r),
            self.specifics[rep],
            _constraint_name[self.constraints[rep]]))

      constraint.append(self.constraints[rep])

      for t in self.sets.subset(rep):
        assert t not in context
        context[t] = tyvar

    # note equal widths
    width_equality = {}
    for (a,b) in self.width_equalities:
      assert a != b
      va = tyvars[a]
      vb = tyvars[b]
      if va > vb: va,vb = vb,va

      if vb in width_equality:
        width_equality[va] = width_equality[vb]

      width_equality[vb] = va

    # set up the default type
    if self.default_rep is None:
      default_id = len(constraint)
      constraint.append(INT)
      specific[default_id] = predicate_default
    else:
      default_id = tyvars[self.default_rep]

    # NOTE: Ensuring the model includes a default type allows precondition
    # inference to test things like width(%x) > 1, at the cost of making the
    # model slightly bigger and type vector generation slightly slower. Other
    # possible designs include:
    # - Allowing mutation in the AbstractTypeModel, so that a default can be
    #   added if necessary
    # - A flag value in 'context' indicating a default type. This complicates
    #   SMT translation and gets convoluted to handle when generating the
    #   AbstractTypeModel

    return AbstractTypeModel(constraint, specific, min_width, lower_bounds,
      width_equality, default_id)

class AbstractTypeModel(object):
  """Contains the constraints gathered during type checking.
  """

  pointer_width = 64
    # in principle, this could be a parameter to the model, or even vary during
    # enumeration, but right now pointer width doesn't affect anything

  float_tys = (HalfType(), SingleType(), DoubleType())

  def __init__(self, constraint, specific, min_width, lower_bounds,
      width_equality, default_id):
    self.constraint = constraint
    self.specific = specific
    self.min_width = min_width
    self.lower_bounds = lower_bounds
    self.width_equality = width_equality
    self.default_id = default_id
    self.tyvars = len(constraint)

  # TODO: we probably need some way to determine which types are larger/smaller than

  @staticmethod
  def int_types(min_width, max_width):
    """Generate IntTypes in the range min_width to max_width-1.
    """

    if min_width <= 4 < max_width:
      yield IntType(4)
    if min_width <= 8 < max_width:
      yield IntType(8)
    for w in xrange(min_width, min(max_width, 4)):
      yield IntType(w)
    for w in xrange(max(min_width, 5), min(max_width, 8)):
      yield IntType(w)
    for w in xrange(max(min_width, 9), max_width):
      yield IntType(w)

  def floor(self, vid, vector):
    if vid in self.lower_bounds:
      floor = max(vector[v] for v in self.lower_bounds[vid])
    else:
      floor = 0
    floor = max(floor, self.min_width.get(vid, 0))
    return floor

  def bits(self, ty):
    """Return the size of the type in bits.
    """

    if isinstance(ty, IntType):
      return ty.width
    if isinstance(ty, X86FP80Type):
      return 80
    if isinstance(ty, FloatType):
      return ty.exp + ty.frac
      # true for all current floats: the sign bit and the implicit fraction
      # bit cancel out
    if isinstance(ty, PtrType):
      return self.pointer_width

    assert False


  # this could be done as a stack, instead of as nested generators
  def _enum_vectors(self, vid, vector, int_limit):
    if vid >= self.tyvars:
      yield tuple(vector)
      return

    if vid in self.specific:
      # TODO: better to put an upper bound on variables less than a fixed type
      if vector[vid] <= self.floor(vid, vector):
        return

      # this check could be avoided if fixed types occurred before variables
      if vid in self.width_equality and \
          self.bits(vector[vid]) != self.bits(vector[self.width_equality[vid]]):
        return

      for v in self._enum_vectors(vid+1, vector, int_limit):
        yield v

      return

    con = self.constraint[vid]
    if con == FIRST_CLASS:
      tys = itertools.chain(self.int_types(1, int_limit), (PtrType(),), self.float_tys)
    elif con == NUMBER:
      tys = itertools.chain(self.int_types(1, int_limit), self.float_tys)
    elif con == FLOAT:
      tys = (t for t in self.float_tys if t > self.floor(vid, vector))
    elif con == INT_PTR:
      tys = itertools.chain(self.int_types(1, int_limit), (PtrType(),))
    elif con == INT:
      floor = self.floor(vid, vector)
      if isinstance(floor, IntType): floor = floor.width
      tys = self.int_types(floor + 1, int_limit)
    elif con == BOOL:
      tys = (IntType(1),)
    else:
      assert False

    if vid in self.width_equality:
      bits = self.bits(vector[self.width_equality[vid]])
      tys = (t for t in tys if self.bits(t) == bits)
      # NOTE: this wastes a lot of effort, but it's only used for bitcast
      # NOTE: this will permit bitcasting between equal types (eg. i64 -> i64)

    for t in tys:
      vector[vid] = t
      for v in self._enum_vectors(vid+1, vector, int_limit):
        yield v

  def type_vectors(self, int_limit=config.int_limit):
    """Generate type vectors consistent with this model."""

    vector = [None] * self.tyvars

    for vid,ty in self.specific.iteritems():
      vector[vid] = ty

    return self._enum_vectors(0, vector, int_limit)

  def width_equal_tyvars(self, v1, v2):
    """Test whether the type variables are width-equal.
    """

    if v1 > v2:
      v1,v2 = v2,v1

    while v2 in self.width_equality:
      v2 = self.width_equality[v2]
      if v1 == v2:
        return True

    return False

  def transitive_lower_bounds(self, tyvar):
    seen = {}

    def visit(tyvar):
      if tyvar in seen: return

      for v in self.lower_bounds.get(tyvar, []):
        yield v
        for v2 in visit(v):
          yield v2

    return visit(tyvar)

  def extend(self, term):
    """Type-check a term in terms of this model and note variables in the
    global context.

    The term must not introduce new type variables or futher constrain types.
    """

    tc = _ModelExtender(model=self)
    defaultable = []
    for t in subterms(term):
      t.type_constraints(tc)

      # Note any defaultable terms for later
      # (This duplicates logic in Transform; better to centralize this in
      # type_constraints)
      if isinstance(t, (Comparison, FunPred)):
        defaultable.extend(t.args())

    # check if any terms can be defaulted
    logger.debug('defaultable: %s', defaultable)
    for t in defaultable:
      rep = tc.sets.rep(t)
      if rep not in tc.rep_tyvar:
        tc.default(rep)

    for rep in tc.sets.reps():
      # make sure every rep has been associated with a tyvar
      # FIXME: specifics in extension could potentially be unified
      if rep not in tc.rep_tyvar:
        raise Error('Ambiguous type for {}'.format(_name(rep)))

      tyvar = tc.rep_tyvar[rep]

      c = self.constraint[tyvar]
      cx = tc.constraints[rep]
      if most_specific(c, cx) != c:
        raise Error("Constraints too strong for {}".format(_name(term)))

      if rep in tc.specifics:
        if tyvar not in self.specific:
          raise Error("Constraints too strong for {}".format(_name(term)))

        if tc.specifics[rep] != self.specific[tyvar]:
          raise Error("Incompatible constraints for {}".format(_name(term)))

    tc.simplify_orderings()

    # check width equalities
    for (t1,t2) in tc.width_equalities:
      if t1 == t2:
        raise Error("Improperly unified {} and {}".format(_name(t1), _name(t2)))

      if not self.width_equal_tyvars(tc.rep_tyvar[t1], tc.rep_tyvar[t2]):
        raise Error("Constraints too strong for " + _name(term))

    # check width inequalities
    for (lo,hi) in tc.ordering:
      v2 = tc.rep_tyvar[hi]
      if isinstance(lo, int):
        if lo > self.min_width.get(v2,0) and \
            all(lo > self.min_width.get(v,0)
              for v in self.transitive_lower_bounds(v2)) and \
            (v2 not in self.specific or lo > self.bits(self.specific[v2])):
          raise Error("Constraints too strong for " + _name(term))
      else:
        v1 = tc.rep_tyvar[lo]

        if all(v != v1 for v in self.transitive_lower_bounds(v2)):
          raise Error("Constraints too strong for " + _name(term))

    # assign tyvars to the new terms
    for rep, terms in tc.sets.subset_items():
      tyvar = tc.rep_tyvar[rep]

      for t in terms:
        assert t not in context or context[t] == tyvar
        context[t] = tyvar

def _name(term):
  return term.name if hasattr(term, 'name') else str(term)

class _ModelExtender(TypeConstraints):
  """Used by AbstractTypeModel.extend.
  """
  logger = logger.getChild('_ModelExtender')

  def __init__(self, model, **kws):
    self.model = model
    self.tyvar_reps = [None] * model.tyvars
    self.rep_tyvar = {}
    super(_ModelExtender, self).__init__(**kws)

  def _init_term(self, term):
    super(_ModelExtender, self)._init_term(term)

    if term not in context:
      return

    tyvar = context[term]
    rep = self.tyvar_reps[tyvar]
    if rep:
      self.sets.unify(term, rep, self._merge)
    else:
      self.logger.debug('Set rep for tyvar %s to %s', tyvar, term)
      self.tyvar_reps[tyvar] = term
      self.rep_tyvar[term] = tyvar

  def _merge(self, t1, t2):
    super(_ModelExtender, self)._merge(t1, t2)

    if t2 in self.rep_tyvar:
      if t1 in self.rep_tyvar:
        raise Error('Cannot unify types for {} and {}'.format(
          _name(t1), _name(t2)))

      tyvar = self.rep_tyvar.pop(t2)
      self.rep_tyvar[t1] = tyvar
      self.tyvar_reps[tyvar] = t1
      self.logger.debug('Set rep for tyvar %s to %s', tyvar, t1)

  def _init_default(self, rep):
    super(_ModelExtender, self)._init_default(rep)
    tyvar = self.model.default_id

    assert rep not in self.rep_tyvar
    assert self.tyvar_reps[tyvar] is None
    self.rep_tyvar[rep] = tyvar
    self.tyvar_reps[tyvar] = rep

class Validator(object):
  """Compare type constraints for a term against a supplied type vector.

  Usage:
    given an AbstractTypeModel m, a type vector v, and a term t,

    > t.type_constraints(Validator(m,v))

    will return None for success and raise an Error if the term's constraints
    are not met.
  """

  def __init__(self, type_model, type_vector):
    self.type_model = type_model
      # just needed for bits in width_equal
    self.type_vector = type_vector

  def type(self, term):
    return self.type_vector[context[term]]

  def eq_types(self, *terms):
    it = iter(terms)
    t1 = it.next()
    ty = self.type(t1)

    for t in it:
      if self.type(t) != ty:
        raise Error

  def specific(self, term, ty):
    if ty is not None and self.type(term) != ty:
      raise Error

  def integer(self, term):
    if not isinstance(self.type(term), IntType):
      raise Error

  def bool(self, term):
    if self.type(term) != IntType(1):
      raise Error

  def pointer(self, term):
    if not isinstance(self.type(term), PtrType):
      raise Error

  def int_ptr_vec(self, term):
    if not isinstance(self.type(term), (IntType, PtrType)):
      raise Error

  def float(self, term):
    if not isinstance(self.type(term), FloatType):
      raise Error

  def first_class(self, term):
    if not isinstance(self.type(term), (IntType, FloatType, PtrType)):
      raise Error

  def number(self, term):
    if not isinstance(self.type(term), (IntType, FloatType)):
      raise Error

  def width_order(self, lo, hi):
    if isinstance(lo, Value):
      lo = self.type(lo)

    if lo >= self.type(hi):
      raise Error

  def width_equal(self, a, b):
    if self.type_model.bits(self.type(a)) != self.type_model.bits(self.type(b)):
      raise Error

class Error(Exception):
  pass
