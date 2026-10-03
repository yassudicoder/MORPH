"""A minimal behavior tree: Selector, Sequence, Condition, Action.

Each robot runs one of these every simulation step. The name of the branch
that ran is shown in the console, so you can see when a robot falls back to
its local playbook.
"""
SUCCESS, FAILURE, RUNNING = "SUCCESS", "FAILURE", "RUNNING"


class Node:
    def __init__(self, name):
        self.name = name


class Selector(Node):
    """Try children in order; the first one that does not fail wins."""

    def __init__(self, name, children):
        super().__init__(name)
        self.children = children

    def tick(self, bb):
        for c in self.children:
            r = c.tick(bb)
            if r != FAILURE:
                return r
        return FAILURE


class Sequence(Node):
    """Run children in order; stop at the first one that is not a success."""

    def __init__(self, name, children):
        super().__init__(name)
        self.children = children

    def tick(self, bb):
        for c in self.children:
            r = c.tick(bb)
            if r != SUCCESS:
                return r
        bb.branch = self.name
        return SUCCESS


class Condition(Node):
    def __init__(self, name, fn):
        super().__init__(name)
        self.fn = fn

    def tick(self, bb):
        return SUCCESS if self.fn() else FAILURE


class Action(Node):
    def __init__(self, name, fn, branch=None):
        super().__init__(name)
        self.fn = fn
        self.branch = branch or name

    def tick(self, bb):
        r = self.fn()
        if r != FAILURE:
            bb.branch = self.branch
        return r
