# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import logging
import threading
from collections import defaultdict

from pants.base.specs import DescendantAddresses, SiblingAddresses, SingleAddress
from pants.build_graph.address import Address
from pants.engine.exp.addressable import Addresses
from pants.engine.exp.fs import PathGlobs
from pants.engine.exp.nodes import (DependenciesNode, FilesystemNode, Node, Noop, ProjectionNode,
                                    Return, SelectNode, State, StepContext, TaskNode, Throw,
                                    Waiting)
from pants.engine.exp.objects import Closable
from pants.engine.exp.selectors import (Select, SelectDependencies, SelectLiteral, SelectProjection,
                                        SelectVariant)
from pants.util.objects import datatype


logger = logging.getLogger(__name__)


class ProductGraph(object):

  class Entry(object):
    __slots__ = ('node', 'state', 'level', 'dependencies', 'dependents', 'cyclic_dependencies')

    def __init__(self, node):
      self.node = node
      # The computed value for a Node: if a Node hasn't been computed yet, it will be None.
      self.state = None
      # Level for cycle detection. Levels represent a pseudo-topological ordering of Nodes.
      self.level = 1
      # Sets of dependency/dependent Nodes.
      self.dependencies = set()
      self.dependents = set()
      # Illegal/cyclic dependencies. We prevent cyclic dependencies from being introduced into the
      # dependencies/dependents lists themselves, but track them independently in order to provide
      # context specific error messages when they are introduced.
      self.cyclic_dependencies = set()

  def __init__(self, validator=None):
    self._validator = validator or Node.validate_node
    # A dict of Node->Entry.
    self._nodes = dict()
    # The total count of non-cyclic edges is used to bound cycle searches.
    self._edge_count = 0

  def __len__(self):
    return len(self._nodes)

  def is_complete(self, node):
    entry = self._nodes.get(node, None)
    if not entry:
      return False
    return entry.state is not None

  def state(self, node):
    entry = self._nodes.get(node, None)
    if not entry:
      return None
    return entry.state

  def update_state(self, node, state):
    """Updates the Node with the given State, creating any Nodes which do not already exist."""
    self._validator(node)
    entry = self._nodes.get(node, None)
    if not entry:
      self._nodes[node] = entry = self.Entry(node)
    if entry.state is not None:
      raise ValueError('Node {} is already completed:\n  {}\n  {}'
                       .format(node, entry.state, state))

    if type(state) in [Return, Throw, Noop]:
      entry.state = state
    elif type(state) is Waiting:
      self._add_dependencies(node, entry, state.dependencies)
    else:
      raise State.raise_unrecognized(state)

  def _level(self, node):
    # TODO: remove in favor of tighter integration in _detect_cycle.
    entry = self._nodes.get(node, None)
    if not entry:
      return 1
    return entry.level

  def _detect_cycle(self, v, w):
    """Given a src (v) and a dest (w), each of which _might_ exist in the graph, detect cycles.

    Implements the sparse-graph algorithm from:
      "A New Approach to Incremental Cycle Detection and Related Problems"
        - Bender, Fineman, Gilbert, Tarjan

    # TODO: The mutation of levels would likely cause inconsistencies in case of a cycle.
    # see whether we need them at all.

    Returns True if a cycle would be created by adding an edge from v->w.
    """

    # delta = min(m^(1/2), n^(2/3))
    delta = min(self._edge_count**(1/2), len(self)**(2/3))
    def same_level_as(node):
      node_level = self._level(node)
      return lambda candidate, _: self._level(candidate) == node_level

    # Step 1
    if self._level(v) < self._level(w):
      # Step 4: no cycle will be created: return.
      return False

    def set_w_level(level):
      # Since w may not exist before this algorithm begins, setting its level involves
      # potentially initializing its entry.
      self._nodes.setdefault(w, self.Entry(w)).level = level

    B = {v}

    # Step 2: search backward within the same level.
    #   2.a: If w is visited, stop and report a cycle.
    #   2.b: If the search completes without traversing at least `delta` arcs and
    #     k(w) = k(v), go to Step 4 (the levels remain a pseudo topological ordering).
    #   2.c: If the search completes without traversing at least `delta` arcs and
    #     k(w) < k(v), set k(w) = k(v).
    #   2.d: If the search traverses at least delta arcs, set k(w) = k(v) + 1 and B = {v}.
    traversed = 0
    for n, _ in self.walk([v], predicate=same_level_as(v), dependents=True):
      if n == v:
        continue
      if n == w:
        # 2.a: Adding v->w would create a cycle.
        return True
      B.add(n)
      traversed += 1
      if traversed >= delta:
        break
    if traversed < delta:
      if self._level(v) == self._level(w):
        # 2.b: Levels are stable, and no cycle would be created.
        return False
      else:
        # 2.c: Continue to Step 3.
        set_w_level(self._level(v))
    else:
      # 2.d: We reached the bound for the backward search: continue to Step 3.
      set_w_level(self._level(v) + 1)
      B = {v}

    # Step 3: search forward, traversing only edges that increase the level.
    #   3.a: If y in B, stop and report a cycle.
    #   3.b: If k(x) = k(y), add (x, y) to in(y).
    #   3.c: If k(x) > k(y), set k(y) = k(x), set in(y) = {(x, y)}, and add all arcs
    #     in out(y) to those to be traversed.
    def _walk_forward(x):
      for y in self.dependencies_of(x):
        if y == x:
          continue
        elif y in B:
          # 3.a: a Node reached during the backwards search was also reached during the
          # forward search.
          return True
        elif self._level(x) > self._level(y):
          # 3.c: Update the levels and traverse the edge.
          self._nodes[y].level = self._level(x)
          if _walk_forward(y):
            return True
          else:
            pass
        else:
          # 3.b: Our 'in' set is computed by filtering the dependencies list; pass.
          pass
      return False

    return _walk_forward(w)

  def _add_dependencies(self, node, entry, dependencies):
    """Adds dependency edges from the given src Node to the given dependency Nodes.

    Executes cycle detection: if adding one of the given dependencies would create
    a cycle, then the _source_ Node is marked as a Noop with an error indicating the
    cycle path, and the dependencies are not introduced.
    """

    def _add_dependent(dependency):
      self._nodes.setdefault(dependency, self.Entry(dependency)).dependents.add(node)

    # Add deps. Any deps which would cause a cycle are added to cyclic_dependencies instead,
    # and ignored except for the purposes of Step execution.
    for dependency in dependencies:
      if dependency in entry.dependencies:
        continue
      self._validator(dependency)
      if self._detect_cycle(node, dependency):
        entry.cyclic_dependencies.add(dependency)
      else:
        self._edge_count += 1
        entry.dependencies.add(dependency)
        _add_dependent(dependency)

  def completed_nodes(self):
    """In linear time, yields the states of any Nodes which have completed."""
    for node, entry in self._nodes.items():
      if entry.state is not None:
        yield node, entry.state

  def dependents(self):
    """Yields the dependents lists for all Nodes."""
    for node, entry in self._nodes.items():
      yield node, entry.dependents

  def dependencies(self):
    """Yields the dependencies lists for all Nodes."""
    for node, entry in self._nodes.items():
      yield node, entry.dependencies

  def cyclic_dependencies(self):
    """Yields the cyclic_dependencies lists for all Nodes."""
    for node, entry in self._nodes.items():
      yield node, entry.cyclic_dependencies

  def dependents_of(self, node):
    entry = self._nodes.get(node, None)
    if not entry:
      return tuple()
    return entry.dependents

  def dependencies_of(self, node):
    entry = self._nodes.get(node, None)
    if not entry:
      return tuple()
    return entry.dependencies

  def cyclic_dependencies_of(self, node):
    entry = self._nodes.get(node, None)
    if not entry:
      return tuple()
    return entry.cyclic_dependencies

  def invalidate(self, predicate=None):
    """Invalidate nodes and their subgraph of dependents given a predicate.

    :param func predicate: A predicate that matches Node objects for all nodes in the graph.
    """
    def _sever_dependents(node):
      for associated in self.dependencies_of(node):
        self.dependents_of(associated).discard(node)

    def _delete_node(node):
      entry = self._nodes.pop(node)
      self._edge_count -= len(entry.dependencies)

    def all_predicate(node, state): return True
    predicate = predicate or all_predicate

    invalidated_roots = list(node for node, entry in self._nodes.items()
                             if predicate(node, entry.state))
    invalidated_nodes = list(node for node, _ in self.walk(roots=invalidated_roots,
                                                           predicate=all_predicate,
                                                           dependents=True))

    # Sever dependee->dependent relationships in the graph for all given invalidated nodes.
    for node in invalidated_nodes:
      _sever_dependents(node)

    # Delete all nodes based on a backwards walk of the graph from all matching invalidated roots.
    for node in invalidated_nodes:
      logger.debug('invalidating node: %r', node)
      _delete_node(node)

    invalidated_count = len(invalidated_nodes)
    logger.info('invalidated {} nodes'.format(invalidated_count))
    return invalidated_count

  def invalidate_files(self, filenames):
    """Given a set of changed filenames, invalidate all related FilesystemNodes in the graph."""
    subjects = set(FilesystemNode.generate_subjects(filenames))
    logger.debug('generated invalidation subjects: %s', subjects)

    def predicate(node, state):
      return type(node) is FilesystemNode and node.subject in subjects

    return self.invalidate(predicate)

  def walk(self, roots, predicate=None, dependents=False):
    """Yields Nodes and their States depth-first in pre-order, starting from the given roots.

    Each node entry is a tuple of (Node, State).

    The given predicate is applied to entries, and eliminates the subgraphs represented by nodes
    that don't match it. The default predicate eliminates all `Noop` subgraphs.
    """
    def _default_walk_predicate(node, state):
      return type(state) is not Noop
    predicate = predicate or _default_walk_predicate

    walked = set()
    def _walk(nodes):
      for node in nodes:
        if node in walked:
          continue
        walked.add(node)
        entry = self._nodes[node]
        if not predicate(node, entry.state):
          continue

        yield (node, entry.state)
        for e in _walk(entry.dependents if dependents else entry.dependencies):
          yield e

    for entry in _walk(roots):
      yield entry

  def visualize(self, roots):
    """Visualize a graph walk by generating graphviz `dot` output.

    :param iterable roots: An iterable of the root nodes to begin the graph walk from.
    """
    viz_colors = {}
    viz_color_scheme = 'set312'  # NB: There are only 12 colors in `set312`.
    viz_max_colors = 12

    def format_color(node, node_state):
      if type(node_state) is Throw:
        return 'tomato'
      elif type(node_state) is Noop:
        return 'white'
      return viz_colors.setdefault(node.product, (len(viz_colors) % viz_max_colors) + 1)

    def format_type(node):
      return node.func.__name__ if type(node) is TaskNode else type(node).__name__

    def format_subject(node):
      if node.variants:
        return '({})@{}'.format(node.subject,
                                ','.join('{}={}'.format(k, v) for k, v in node.variants))
      else:
        return '({})'.format(node.subject)

    def format_product(node):
      if type(node) is SelectNode and node.variant_key:
        return '{}@{}'.format(node.product.__name__, node.variant_key)
      return node.product.__name__

    def format_node(node, state):
      return '{}:{}:{} == {}'.format(format_product(node),
                                     format_subject(node),
                                     format_type(node),
                                     str(state).replace('"', '\\"'))

    yield 'digraph plans {'
    yield '  node[colorscheme={}];'.format(viz_color_scheme)
    yield '  concentrate=true;'
    yield '  rankdir=LR;'

    for (node, node_state) in self.walk(roots):
      node_str = format_node(node, node_state)

      yield ' "{}" [style=filled, fillcolor={}];'.format(node_str, format_color(node, node_state))

      for dep in self.dependencies_of(node):
        dep_state = self.state(dep)
        if type(dep_state) is Noop:
          continue
        yield '  "{}" -> "{}"'.format(node_str, format_node(dep, dep_state))

    yield '}'


class ExecutionRequest(datatype('ExecutionRequest', ['roots'])):
  """Holds the roots for an execution, which might have been requested by a user.

  To create an ExecutionRequest, see `LocalScheduler.build_request` (which performs goal
  translation) or `LocalScheduler.execution_request`.

  :param roots: Root Nodes for this request.
  :type roots: list of :class:`pants.engine.exp.nodes.Node`
  """


class Promise(object):
  """An extremely simple _non-threadsafe_ Promise class."""

  def __init__(self):
    self._success = None
    self._failure = None
    self._is_complete = False

  def is_complete(self):
    return self._is_complete

  def success(self, success):
    self._success = success
    self._is_complete = True

  def failure(self, exception):
    self._failure = exception
    self._is_complete = True

  def get(self):
    """Returns the resulting value, or raises the resulting exception."""
    if not self._is_complete:
      raise ValueError('{} has not been completed.'.format(self))
    if self._failure:
      raise self._failure
    else:
      return self._success


class NodeBuilder(Closable):
  """Holds an index of tasks used to instantiate TaskNodes."""

  @classmethod
  def create(cls, tasks):
    """Indexes tasks by their output type."""
    serializable_tasks = defaultdict(set)
    for output_type, input_selects, task in tasks:
      serializable_tasks[output_type].add((task, tuple(input_selects)))
    return cls(serializable_tasks)

  def __init__(self, tasks):
    self._tasks = tasks

  def gen_nodes(self, subject, product, variants):
    if FilesystemNode.is_filesystem_pair(type(subject), product):
      # Native filesystem operations.
      yield FilesystemNode(subject, product, variants)
    else:
      # Tasks.
      for task, anded_clause in self._tasks[product]:
        yield TaskNode(subject, product, variants, task, anded_clause)

  def select_node(self, selector, subject, variants):
    """Constructs a Node for the given Selector and the given Subject/Variants.

    This method is decoupled from Selector classes in order to allow the `selector` package to not
    need a dependency on the `nodes` package.
    """
    selector_type = type(selector)
    if selector_type is Select:
      return SelectNode(subject, selector.product, variants, None)
    elif selector_type is SelectVariant:
      return SelectNode(subject, selector.product, variants, selector.variant_key)
    elif selector_type is SelectDependencies:
      return DependenciesNode(subject, selector.product, variants, selector.deps_product, selector.field)
    elif selector_type is SelectProjection:
      return ProjectionNode(subject, selector.product, variants, selector.projected_subject, selector.fields, selector.input_product)
    elif selector_type is SelectLiteral:
      # NB: Intentionally ignores subject parameter to provide a literal subject.
      return SelectNode(selector.subject, selector.product, variants, None)
    else:
      raise ValueError('Unrecognized Selector type "{}" for: {}'.format(selector_type, selector))


class StepRequest(datatype('Step', ['step_id', 'node', 'dependencies', 'project_tree'])):
  """Additional inputs needed to run Node.step for the given Node.

  TODO: See docs on StepResult.

  :param step_id: A unique id for the step, to ease comparison.
  :param node: The Node instance that will run.
  :param dependencies: The declared dependencies of the Node from previous Waiting steps.
  :param project_tree: A FileSystemProjectTree instance.
  """

  def __call__(self, node_builder):

    """Called by the Engine in order to execute this Step."""
    step_context = StepContext(node_builder, self.project_tree)
    state = self.node.step(self.dependencies, step_context)
    return (StepResult(state,))

  def __eq__(self, other):
    return type(self) == type(other) and self.step_id == other.step_id

  def __ne__(self, other):
    return not (self == other)

  def __hash__(self):
    return hash(self.step_id)


class StepResult(datatype('Step', ['state'])):
  """The result of running a Step, passed back to the Scheduler via the Promise class.

  :param state: The State value returned by the Step.
  """


class LocalScheduler(object):
  """A scheduler that expands a ProductGraph by executing user defined tasks."""

  def __init__(self, goals, tasks, storage, project_tree, graph_lock=None, graph_validator=None):
    """
    :param goals: A dict from a goal name to a product type. A goal is just an alias for a
           particular (possibly synthetic) product.
    :param tasks: A set of (output, input selection clause, task function) triples which
           is used to compute values in the product graph.
    :param project_tree: An instance of ProjectTree for the current build root.
    :param graph_lock: A re-entrant lock to use for guarding access to the internal ProductGraph
                       instance. Defaults to creating a new threading.RLock().
    """
    self._products_by_goal = goals
    self._tasks = tasks
    self._project_tree = project_tree
    self._node_builder = NodeBuilder.create(self._tasks)

    self._graph_validator = graph_validator
    self._product_graph = ProductGraph()
    self._product_graph_lock = graph_lock or threading.RLock()
    self._step_id = 0

  def visualize_graph_to_file(self, roots, filename):
    """Visualize a graph walk by writing graphviz `dot` output to a file.

    :param iterable roots: An iterable of the root nodes to begin the graph walk from.
    :param str filename: The filename to output the graphviz output to.
    """
    with self._product_graph_lock, open(filename, 'wb') as fh:
      for line in self.product_graph.visualize(roots):
        fh.write(line)
        fh.write('\n')

  def _create_step(self, node):
    """Creates a Step and Promise with the currently available dependencies of the given Node.

    If the dependencies of a Node are not available, returns None.

    TODO: Content addressing node and its dependencies should only happen if node is cacheable
      or in a multi-process environment.
    """
    Node.validate_node(node)

    # See whether all of the dependencies for the node are available.
    deps = dict()
    for dep in self._product_graph.dependencies_of(node):
      state = self._product_graph.state(dep)
      if state is None:
        return None
      deps[dep] = state
    # Additionally, include Noops for any dependencies that were cyclic.
    for dep in self._product_graph.cyclic_dependencies_of(node):
      noop_state = Noop('Dep from {} to {} would cause a cycle.'.format(node, dep))
      deps[dep] = noop_state

    # Ready.
    self._step_id += 1
    return (StepRequest(self._step_id, node, deps, self._project_tree), Promise())

  def node_builder(self):
    """Return the NodeBuilder instance for this Scheduler.

    A NodeBuilder is a relatively heavyweight object (since it contains an index of all
    registered tasks), so it should be used for the execution of multiple Steps.
    """
    return self._node_builder

  def build_request(self, goals, subjects):
    """Translate the given goal names into product types, and return an ExecutionRequest.

    :param goals: The list of goal names supplied on the command line.
    :type goals: list of string
    :param subjects: A list of Spec and/or PathGlobs objects.
    :type subject: list of :class:`pants.base.specs.Spec`, `pants.build_graph.Address`, and/or
      :class:`pants.engine.exp.fs.PathGlobs` objects.
    :returns: An ExecutionRequest for the given goals and subjects.
    """
    return self.execution_request([self._products_by_goal[goal_name] for goal_name in goals],
                                  subjects)

  def execution_request(self, products, subjects):
    """Create and return an ExecutionRequest for the given products and subjects.

    The resulting ExecutionRequest object will contain keys tied to this scheduler's ProductGraph, and
    so it will not be directly usable with other scheduler instances without being re-created.

    An ExecutionRequest for an Address represents exactly one product output, as does SingleAddress. But
    we differentiate between them here in order to normalize the output for all Spec objects
    as "list of product".

    :param products: A list of product types to request for the roots.
    :type products: list of types
    :param subjects: A list of Spec and/or PathGlobs objects.
    :type subject: list of :class:`pants.base.specs.Spec`, `pants.build_graph.Address`, and/or
      :class:`pants.engine.exp.fs.PathGlobs` objects.
    :returns: An ExecutionRequest for the given products and subjects.
    """

    # Determine the root Nodes for the products and subjects selected by the goals and specs.
    def roots():
      for subject in subjects:
        for product in products:
          if type(subject) is Address:
            yield SelectNode(subject, product, None, None)
          elif type(subject) in [SingleAddress, SiblingAddresses, DescendantAddresses]:
            yield DependenciesNode(subject, product, None, Addresses, None)
          elif type(subject) is PathGlobs:
            yield DependenciesNode(subject, product, None, subject.ftype, None)
          else:
            raise ValueError('Unsupported root subject type: {}'.format(subject))

    return ExecutionRequest(tuple(roots()))

  @property
  def product_graph(self):
    return self._product_graph

  def root_entries(self, execution_request):
    """Returns the roots for the given ExecutionRequest as a dict from Node to State."""
    with self._product_graph_lock:
      return {root: self._product_graph.state(root) for root in execution_request.roots}

  def _complete_step(self, node, step_result):
    """Given a StepResult for the given Node, complete the step."""
    result = step_result.state
    # Update the Node's state in the graph.
    self._product_graph.update_state(node, result)

  def invalidate_files(self, filenames):
    """Calls `ProductGraph.invalidate_files()` against an internal ProductGraph instance
    under protection of a scheduler-level lock."""
    with self._product_graph_lock:
      return self._product_graph.invalidate_files(filenames)

  def schedule(self, execution_request):
    """Yields batches of Steps until the roots specified by the request have been completed.

    This method should be called by exactly one scheduling thread, but the Step objects returned
    by this method are intended to be executed in multiple threads, and then satisfied by the
    scheduling thread.
    """
    # A dict from Node to a possibly executing Step. Only one Step exists for a Node at a time.
    outstanding = {}
    # Nodes that might need to have Steps created (after any outstanding Step returns).
    candidates = set(execution_request.roots)

    with self._product_graph_lock:
      # Yield nodes that are ready, and then compute new ones.
      scheduling_iterations = 0
      while True:
        # Create Steps for candidates that are ready to run, and not already running.
        ready = dict()
        for candidate_node in list(candidates):
          if candidate_node in outstanding:
            # Node is still a candidate, but is currently running.
            continue
          if self._product_graph.is_complete(candidate_node):
            # Node has already completed.
            candidates.discard(candidate_node)
            continue
          # Create a step if all dependencies are available; otherwise, can assume they are
          # outstanding, and will cause this Node to become a candidate again later.
          candidate_step = self._create_step(candidate_node)
          if candidate_step is not None:
            ready[candidate_node] = candidate_step
          candidates.discard(candidate_node)

        if not ready and not outstanding:
          # Finished.
          break
        yield ready.values()
        scheduling_iterations += 1
        outstanding.update(ready)

        # Finalize completed Steps.
        for node, entry in outstanding.items()[:]:
          step, promise = entry
          if not promise.is_complete():
            continue
          # The step has completed; see whether the Node is completed.
          outstanding.pop(node)
          self._complete_step(step.node, promise.get())
          if self._product_graph.is_complete(step.node):
            # The Node is completed: mark any of its dependents as candidates for Steps.
            candidates.update(d for d in self._product_graph.dependents_of(step.node))
          else:
            # Waiting on dependencies.
            incomplete_deps = [d for d in self._product_graph.dependencies_of(step.node)
                               if not self._product_graph.is_complete(d)]
            if incomplete_deps:
              # Mark incomplete deps as candidates for Steps.
              candidates.update(incomplete_deps)
            else:
              # All deps are already completed: mark this Node as a candidate for another step.
              candidates.add(step.node)

      logger.info('visited {} nodes in {} scheduling iterations. '
                  'there have been {} total steps for {} total nodes.'.format(
                    sum(1 for _ in self._product_graph.walk(execution_request.roots)),
                    scheduling_iterations,
                    self._step_id,
                    len(self._product_graph)),)

      if self._graph_validator is not None:
        self._graph_validator.validate(self._product_graph)
