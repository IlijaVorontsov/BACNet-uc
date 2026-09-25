"""A configurable role for engine and transaction tests."""

from uc_controller.roles.base import Probe, ProbeResult, RenderedFile, Role, Validator


class DummyRole(Role):
    def __init__(self, name, order=50, files=None, units=None, validators=(), probe_ok=True,
                 enabled=True, enable=None):
        self.name = name
        self.order = order
        self.files = dict(files or {})          # path -> text or RenderedFile
        self.units = dict(units or {})
        self._validators = list(validators)
        self.probe_ok = probe_ok
        self._enabled = enabled
        self._enable = dict(enable or {})
        self.events = []

    def enabled(self, ctx):
        return self._enabled

    def render(self, ctx):
        out = []
        for path, value in self.files.items():
            if isinstance(value, RenderedFile):
                out.append(value)
            else:
                out.append(RenderedFile(path, value.replace("@P@", ctx.prefix).encode()))
        return out

    def validators(self, ctx):
        return list(self._validators)

    def prepare(self, ctx, run):
        self.events.append("prepare")

    def post_install(self, ctx, run):
        self.events.append("post_install")

    def enable_units(self, ctx):
        return dict(self._enable)

    def probes(self, ctx):
        ok = self.probe_ok

        def run(ctx, runner):
            self.events.append("probe")
            return ProbeResult(ok() if callable(ok) else ok, "dummy")
        return [Probe(f"{self.name}-probe", run, retries=2, delay_s=0)]


def validator(name, argv, needs=None):
    return Validator(name, argv, needs=needs)
