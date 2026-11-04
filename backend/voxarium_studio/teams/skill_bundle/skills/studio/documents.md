# Writing a shared document

The Studio's Documents section is a block editor over the Markdown file on disk. Anything it cannot
show as a block it keeps byte for byte and shows as an "unsupported block", so write in the dialect
below and the document reads well both in the Studio and in any other Markdown viewer.

## The file

- A document is a `.md` file under the docs folder (`.markdown` and `.txt` work too). Folders group
  documents: `features/dash.md`.
- Its title is the first `#` heading, or the file name when there is none — start with one.
- Write each paragraph as one line, however long. That is how the editor writes it, and it keeps a
  change to one word a one-word diff.
- Match the rest of what the editor writes, so the first save by a person moves nothing: `-` for
  bullets, `*italic*` and `**bold**`, fenced code blocks, `---` for a divider, `#` headings, and
  tables with one space inside each cell and `| --- |` delimiters.
- Frontmatter at the top is kept as written.
- No raw HTML: it is shown as text in an unsupported block, never rendered.

## Blocks

| Block | Markdown |
| --- | --- |
| Heading | `#`, `##`, `###` — levels 4 to 6 are kept, but the editor does not offer them |
| Bulleted list | `- item` |
| Numbered list | `1. item` |
| Checklist | `- [ ] open` and `- [x] done` |
| Quote | `> text` |
| Code | a fenced block with its language: ```` ```csharp ```` |
| Table | a GFM table; `:---:` or `---:` in the delimiter row aligns a column |
| Divider | `---` |
| Callout | `:::note[Title]` … `:::` — the kinds are `note`, `tip`, `warning` and `danger` |
| Toggle | `:::toggle[Title]` … `:::` — `{open}` after the title shows it expanded |

The title in brackets is optional on both. Callouts and toggles hold any blocks — lists, code,
tables, other callouts:

```markdown
:::warning[Save data changes]
Saves from before this change lose the dash cooldown. See [[save-format]].
:::

:::toggle[Tuning]{open}
| Parameter | Value |
| --- | ---: |
| Distance | 4.5 |
:::
```

## Inline

Bold, italic, strikethrough (`~~…~~`), `code`, links (`[text](https://…)`), and two of the Studio's
own:

- **Wiki links** — `[[name]]`, or `[[name|label]]` to show other words. `name` is another document's
  file name without the suffix, matched case-insensitively anywhere in the docs folder. When two
  documents share a name, write the path instead: `[[qa/notes]]`. A link to a document that does not
  exist yet is not an error — the Studio offers to create it. Links feed the Backlinks panel and the
  link graph, so link the documents your text depends on.
- **Coloured text** — `[text]{.green}`. The colours are exactly `red`, `orange`, `yellow`, `green`,
  `cyan`, `blue`, `purple`, `pink`, `brown` and `gray`; with any other word the brackets stay literal
  text. A span stays within one line and may hold other inline markup. Use it for status words, and
  sparingly.

There is no underline.

## Pictures

- `![alt](shot.png)` shows `shot.png` from the **document's own folder**. A relative path may reach
  anywhere inside the project: `![hero](../../Assets/hero.png)`.
- `{width=300}` straight after the reference sets the width in whole pixels:
  `![hero](hero.png){width=300}`. No other attribute and no other unit is understood.
- Percent-encode spaces in the path: `![a shot](a%20shot.png)`.
- A remote picture (`https://…`, `data:`) is shown as a link and never loaded.
- `write_doc` writes text only: a picture has to exist as a file already.

## Folders

A folder's colour and icon live in a `vxs-folder.json` inside it. Leave that file alone.
