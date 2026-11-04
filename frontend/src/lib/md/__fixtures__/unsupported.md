---
title: Held byte for byte
owner:   marina        # trailing spaces and odd indentation survive
tags: [design, "combat"]
---

# Unsupported constructs

Everything below is outside §3.1 and must come back unchanged (§8.3).

An image, shown but not editable: ![Dash frames](../assets/dash.png "Frame sheet")

A raw HTML block:

<details>
  <summary>Collapsed by hand</summary>
  Written before the editor had toggles.
</details>

Inline HTML: text with <kbd>Ctrl</kbd> in it.

A footnote reference[^budget].

[^budget]: The ability budget is 12 points per character.

A link definition, used by [the reference][spec] twice, [like so][spec].

[spec]: https://example.invalid/spec "The spec"

An autolink: <https://example.invalid/raw>.

A hard break at the end of this line\
continues here.

Escaped characters that must not be unescaped: \*not emphasis\*, \_not italic\_, \[not a link].
