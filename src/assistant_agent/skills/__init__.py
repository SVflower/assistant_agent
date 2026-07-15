"""Skills 层：可复用的指示书（SKILL.md）。

渐进披露：
- Level 1（本层 discover）：只取 frontmatter 的 name/description，注入系统提示词。
- Level 2（LoadSkillTool）：模型按需加载技能正文。
- Level 3：正文指向的脚本/参考文件，模型用现有工具读/跑，本层不介入。

叶子能力层（rank 2），只依赖 tools/base，不依赖 agent/ui/main。
"""

from __future__ import annotations

from assistant_agent.skills.store import SkillMeta, SkillSource, SkillStore
from assistant_agent.skills.tool import LoadSkillTool

__all__ = ["LoadSkillTool", "SkillMeta", "SkillSource", "SkillStore"]
