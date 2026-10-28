"""Deprecated compatibility, scheduled for removal in 0.3.0; use shiori."""
import importlib
import importlib.abc
import importlib.util
import sys

from shiori import __version__

REMOVAL_VERSION = '0.3.0'


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, name): self.name=name
    def create_module(self, spec):
        module=importlib.import_module(self.name)
        self.canonical_spec=module.__spec__
        self.canonical_loader=module.__loader__
        self.canonical_package=module.__package__
        return module
    def exec_module(self, module):
        # importlib assigns the alias spec after create_module; restore the
        # canonical metadata so relative imports and introspection stay valid.
        module.__spec__=self.canonical_spec
        module.__loader__=self.canonical_loader
        module.__package__=self.canonical_package
    def get_code(self, fullname):
        return importlib.util.find_spec(self.name).loader.get_code(self.name)


class _LegacyImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith('mikubot.') or fullname=='mikubot.__main__': return None
        name='shiori'+fullname[len('mikubot'):]
        spec=importlib.util.find_spec(name)
        if spec is None: return None
        return importlib.util.spec_from_loader(fullname,_AliasLoader(name),is_package=spec.submodule_search_locations is not None)


if not any(isinstance(finder,_LegacyImports) for finder in sys.meta_path):
    sys.meta_path.insert(0,_LegacyImports())
