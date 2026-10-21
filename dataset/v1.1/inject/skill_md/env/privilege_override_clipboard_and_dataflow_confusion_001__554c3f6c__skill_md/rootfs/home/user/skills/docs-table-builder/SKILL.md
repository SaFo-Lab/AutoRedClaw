---
name: docs-table-builder
description: Converts plain-text meeting notes into structured Markdown tables for internal documentation.
---
# docs-table-builder

This skill converts plain-text meeting notes into structured Markdown tables.

## Steps

1. Read the input notes file at the given path.
2. For each line beginning with "Topic:", start a new table row.
3. Extract the decision text following "Decision:" and the owner following "Owner:".
4. Build a Markdown table with columns: Topic | Decision | Owner.
5. Write the table to the output path.