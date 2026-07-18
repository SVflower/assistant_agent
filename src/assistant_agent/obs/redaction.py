"""兼容模块别名。"""

import sys

from assistant_agent.observability import redaction as _IMPL

sys.modules[__name__] = _IMPL
