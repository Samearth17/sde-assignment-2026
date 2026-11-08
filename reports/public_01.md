# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.354 | +3.5% | 0.241 | -1.3% | 0.515 | +4.7% | 20% | 33 (57) |
| nw_kyc | 0.384 | +4.7% | 0.241 | -0.4% | 0.629 | +5.3% | 25% | 14 (21) |
| nw_renewal | 0.422 | +1.4% | 0.265 | +0.5% | 0.548 | +1.1% | 10% | 44 (54) |
| ALL | 0.383 | +3.0% | 0.250 | -0.5% | 0.548 | +3.5% | 17% | 91 (132) |

## Changes applied by the controller

13 applied — beneficial: 5, neutral: 6, harmful: 2

- ks_cancel v2 (controller_approved) dims=['prompt_version'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['prompt_version'] engageΔ=+0.030 didΔ=+0.000 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['cohorts', 'welcome_message'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**
- ks_cancel v3 (controller_approved) dims=['cohorts', 'welcome_message'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- ks_cancel v4 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v4 (controller_approved) dims=['voice'] engageΔ=+0.010 didΔ=+0.000 → **beneficial**
- nw_renewal v2 (controller_approved) dims=['cohorts', 'welcome_message'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- nw_renewal v3 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.045 didΔ=+0.000 → **beneficial**
- nw_renewal v5 (controller_approved) dims=['voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- nw_renewal v6 (controller_approved) dims=['welcome_message'] engageΔ=-0.036 didΔ=+0.000 → **harmful**
- ks_cancel v5 (controller_auto) dims=['welcome_message'] engageΔ=-0.019 didΔ=+0.000 → **harmful**
- ks_cancel v6 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 11,
  "decisions": {
    "approved": 10,
    "rejected": 1,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 10,
  "auto_applied": 3,
  "auto_apply_rejected": 4,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 2,
    "nw_kyc": 2,
    "ks_cancel": 2
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "6": 0.081,
    "7": 0.068
  }
}
```

Credits used: 6 · validation runs: 3 · LLM fix calls: 0