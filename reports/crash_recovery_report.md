# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.351 | +2.7% | 0.241 | -1.4% | 0.511 | +3.8% | 20% | 37 (57) |
| nw_kyc | 0.385 | +5.1% | 0.247 | +2.1% | 0.616 | +3.2% | 22% | 21 (21) |
| nw_renewal | 0.435 | +4.5% | 0.262 | -1.0% | 0.567 | +4.6% | 14% | 32 (54) |
| ALL | 0.386 | +3.9% | 0.249 | -0.6% | 0.551 | +4.1% | 18% | 90 (132) |

## Changes applied by the controller

14 applied — beneficial: 5, neutral: 8, harmful: 1

- ks_cancel v2 (controller_approved) dims=['prompt_version'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- nw_renewal v2 (controller_approved) dims=['prompt_version'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['prompt_version'] engageΔ=+0.030 didΔ=+0.000 → **beneficial**
- ks_cancel v3 (controller_approved) dims=['cohorts', 'welcome_message'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- ks_cancel v4 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v3 (controller_approved) dims=['cohorts', 'welcome_message'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**
- nw_renewal v3 (controller_approved) dims=['cohorts', 'voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v4 (controller_approved) dims=['voice'] engageΔ=+0.010 didΔ=+0.000 → **beneficial**
- nw_renewal v4 (controller_approved) dims=['voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- nw_renewal v5 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v5 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v6 (controller_auto) dims=['welcome_message'] engageΔ=-0.019 didΔ=+0.000 → **harmful**
- ks_cancel v7 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v6 (controller_approved) dims=['voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 10,
  "decisions": {
    "approved": 9,
    "rejected": 1,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 9,
  "auto_applied": 5,
  "auto_apply_rejected": 3,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 2,
    "nw_kyc": 1,
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
{}
```

Credits used: 6 · validation runs: 3 · LLM fix calls: 0