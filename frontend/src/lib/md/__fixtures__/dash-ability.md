# Dash ability

A short burst of speed. Design owner: Marina. This paragraph is deliberately long enough that a column-wrapping serializer would break it across lines, which is exactly what §8.1 forbids — one edited word must not rewrite the whole paragraph.

## Rules

- Cooldown **2s**, cannot be cancelled
- Grants *i-frames* for 0.2s
- Uses the `ability` budget, see [[test-plan]] and [[combat-tuning]]

### Cancel windows

1. Startup, frames 1-3
2. Active, frames 4-12
3. Recovery, everything after

## Acceptance

- [x] Cooldown respected under lag
- [ ] Animation blends from any locomotion state
- [ ] QA sign-off

| Stat | Value | Notes |
| --- | --- | --- |
| Distance | 6 m | Measured from the capsule origin |
| Cooldown | 2 s | Shared with `Blink` |

> Blocked on the animation set landing first.
> The rig lands in sprint 14.

```python
unreal.EditorAssetLibrary.does_asset_exist("/Game/Abilities/Dash")
```

---

Closing note with ~~a struck clause~~ and a [real link](https://example.invalid/dash).
