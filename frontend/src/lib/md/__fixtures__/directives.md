# Directives

:::note[Plain note]
A callout with a heading.
:::

:::tip
A callout with no heading at all, which §3.3 says renders without a header row.
:::

:::warning[Blocked]
Blocked on the animation set landing first.

- With a list inside
- And a second item

| Column | Column |
| --- | --- |
| a | b |
:::

:::danger[Do not ship]
```ts
const budget = 2
```
:::

:::toggle[Implementation detail]
Hidden content, collapsed by default.
:::

:::toggle[Open by default]{open}
Shown content. `{open}` is the only part of the toggle state that reaches the file — a
click in the UI must not dirty the document.
:::

:::toggle
A toggle with no label at all.
:::

::::note[Outer]
An outer callout that contains another one.

:::tip[Inner]
Nested directives are allowed, including lists and code.
:::
::::
