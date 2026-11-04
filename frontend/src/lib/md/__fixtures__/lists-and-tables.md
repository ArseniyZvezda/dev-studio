# Lists and tables

## Nesting

- Top level
  - Second level
    - Third level
      - Fourth level
- Back to the top

1. Numbered
   1. Nested numbered
   2. And another
2. Second

- [ ] Task with a nested list
  - [x] Done child
  - [ ] Pending child

## Mixed content in an item

- An item with a paragraph.

  A second paragraph inside the same item.

  ```sh
  voxarium_studio info .
  ```

- An item holding a table

  | a | b |
  | --- | --- |
  | 1 | 2 |

## Alignment

| Left | Centre | Right |
| :--- | :---: | ---: |
| a | b | c |
| longer cell | x | 1 |

## A table with pipes and empty cells

| Column | Value |
| --- | --- |
| escaped \| pipe | |
| | only the right |

## Code

```
No language, and a blank line follows.

Still the same block.
```

```json
{
  "indented": true,
  "blank": ""
}
```
