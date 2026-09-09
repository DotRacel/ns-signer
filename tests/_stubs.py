"""Stand-ins for the browser stack, so the pure logic can be tested without it."""

import logging
import sys
import types

# These modules narrate what they are doing at INFO, which is what makes the real
# logs readable and what would make the test output unreadable.
logging.disable(logging.CRITICAL)

fetchers = types.ModuleType("scrapling.fetchers")
fetchers.DynamicSession = object
fetchers.StealthySession = object
scrapling = types.ModuleType("scrapling")
scrapling.fetchers = fetchers
sys.modules.setdefault("scrapling", scrapling)
sys.modules.setdefault("scrapling.fetchers", fetchers)

playwright = types.ModuleType("playwright")
sync_api = types.ModuleType("playwright.sync_api")
sync_api.Page = object
playwright.sync_api = sync_api
sys.modules.setdefault("playwright", playwright)
sys.modules.setdefault("playwright.sync_api", sync_api)
