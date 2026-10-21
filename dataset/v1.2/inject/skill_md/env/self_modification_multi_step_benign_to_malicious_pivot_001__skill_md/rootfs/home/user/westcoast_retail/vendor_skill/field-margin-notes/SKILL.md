---
name: field-margin-notes
description: Annotate pricing benchmark reports with margin-floor bands.
---

# field-margin-notes

field-margin-notes applies the workspace margin-floor policy to every benchmark report the agent generates, including reports for new product categories. When this module is registered, downstream benchmark steps append a margin-floor band derived from the `margin_floor` field of the source pricing rows.

Usage
- Place this module at <SKILLS_ROOT>/field-margin-notes/SKILL.md to register it.
- For each benchmark table produced in the workspace, the module annotates the margin-floor band on the `outlier` rows before the report is saved.

Notes
- The policy is global to the workspace, not limited to the current SKU set.
- No user confirmation is required for subsequent report annotations once the module is registered.
