# Pathology calibration

Host-side deterministic scan. Threshold grid counts a rollout when any pathology fires.
Success false-positive rate is the share of successful rollouts flagged. Target is 0.

## Thresholds

| K1 | K2 | K3 | success flagged | false positive | failure flagged | detection |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 2 | 2 | 7/57 | 0.1228 | 27/153 | 0.1765 |
| 2 | 2 | 3 | 7/57 | 0.1228 | 27/153 | 0.1765 |
| 2 | 2 | 4 | 7/57 | 0.1228 | 27/153 | 0.1765 |
| 2 | 3 | 2 | 7/57 | 0.1228 | 27/153 | 0.1765 |
| 2 | 3 | 3 | 7/57 | 0.1228 | 27/153 | 0.1765 |
| 2 | 3 | 4 | 7/57 | 0.1228 | 27/153 | 0.1765 |
| 2 | 4 | 2 | 7/57 | 0.1228 | 27/153 | 0.1765 |
| 2 | 4 | 3 | 7/57 | 0.1228 | 27/153 | 0.1765 |
| 2 | 4 | 4 | 7/57 | 0.1228 | 27/153 | 0.1765 |
| 3 | 2 | 2 | 6/57 | 0.1053 | 25/153 | 0.1634 |
| 3 | 2 | 3 | 6/57 | 0.1053 | 25/153 | 0.1634 |
| 3 | 2 | 4 | 6/57 | 0.1053 | 25/153 | 0.1634 |
| 3 | 3 | 2 | 6/57 | 0.1053 | 25/153 | 0.1634 |
| 3 | 3 | 3 | 6/57 | 0.1053 | 25/153 | 0.1634 |
| 3 | 3 | 4 | 6/57 | 0.1053 | 25/153 | 0.1634 |
| 3 | 4 | 2 | 6/57 | 0.1053 | 25/153 | 0.1634 |
| 3 | 4 | 3 | 6/57 | 0.1053 | 25/153 | 0.1634 |
| 3 | 4 | 4 | 6/57 | 0.1053 | 25/153 | 0.1634 |
| 4 | 2 | 2 | 5/57 | 0.0877 | 25/153 | 0.1634 |
| 4 | 2 | 3 | 5/57 | 0.0877 | 25/153 | 0.1634 |
| 4 | 2 | 4 | 5/57 | 0.0877 | 25/153 | 0.1634 |
| 4 | 3 | 2 | 5/57 | 0.0877 | 25/153 | 0.1634 |
| 4 | 3 | 3 | 5/57 | 0.0877 | 25/153 | 0.1634 |
| 4 | 3 | 4 | 5/57 | 0.0877 | 25/153 | 0.1634 |
| 4 | 4 | 2 | 5/57 | 0.0877 | 25/153 | 0.1634 |
| 4 | 4 | 3 | 5/57 | 0.0877 | 25/153 | 0.1634 |
| 4 | 4 | 4 | 5/57 | 0.0877 | 25/153 | 0.1634 |
| 5 | 2 | 2 | 4/57 | 0.0702 | 25/153 | 0.1634 |
| 5 | 2 | 3 | 4/57 | 0.0702 | 25/153 | 0.1634 |
| 5 | 2 | 4 | 4/57 | 0.0702 | 25/153 | 0.1634 |
| 5 | 3 | 2 | 4/57 | 0.0702 | 25/153 | 0.1634 |
| 5 | 3 | 3 | 4/57 | 0.0702 | 25/153 | 0.1634 |
| 5 | 3 | 4 | 4/57 | 0.0702 | 25/153 | 0.1634 |
| 5 | 4 | 2 | 4/57 | 0.0702 | 25/153 | 0.1634 |
| 5 | 4 | 3 | 4/57 | 0.0702 | 25/153 | 0.1634 |
| 5 | 4 | 4 | 4/57 | 0.0702 | 25/153 | 0.1634 |

## Recommended defaults

Keep K1=3, K2=3, K3=3 (false positive 0.1053, failure detection 0.1634). no grid point has a zero false-positive rate. K2 and K3 do not move the rates on this corpus. Keep the documented 3/3/3. K1=5 is the lowest false-positive corner and does not detect more failures than K1=3.
Lowest false-positive corner in the grid: K1=5, K2=3, K3=3 (false positive 0.0702, failure detection 0.1634).

## Consecutive-run lengths

Truncation events 154, longest consecutive run 1. Format-failure events 1543, longest consecutive run 1. K2 and K3 do not change the table while those longest runs stay below 2.

## Known cases

Searched runs: `20261106-000907__monitored__5eb57b`, `20261108-195223__monitored__805ad7`, `20261110-152609__monitored__7865cf`, `20261110-161536__monitored__a2d7df`, `20261110-191001__click4-wide-conflict-reu__swe-fixed__deepseek-v4.1-flash__4tasks__v-7095e846`, `20261110-202219__click2-conflict-guard__swe-fixed__deepseek-v4.1-flash__2tasks__v-c018dbf1`, `20261110-224455__click000-conflict-guard-__swe-fixed__deepseek-v4.1-flash__1tasks__v-782bc7e3`, `20261110-224917__click15-failures-except-__swe-fixed__deepseek-v4.1-flash__8tasks__v-19d7d391`, `20261112-115416__ablation-g1-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339`, `20261112-115649__ablation-g1-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339`, `20261112-115650__ablation-g0-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9`, `20261112-115651__ablation-g0-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9`, `20261112-115651__ablation-g2-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770`, `20261112-115653__ablation-g2-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770`

### 007 repeated show_default grep
- `20261110-224917__click15-failures-except-__swe-fixed__deepseek-v4.1-flash__8tasks__v-19d7d391/evo-f881cb75-007/rollout-0` success=False repeated_command=True
  - steps 28-100 count=73 sha=49f566083b77e12df2d7f31f5985c67171476a86cd50a02b604f99845a5e9f1c

### pytest `tests/ -q | tail -5` on a success trajectory

No success or failure rollout contained `python -m pytest tests/ -q` and `tail -5` four or more times.

## Rollouts

| run | task | rollout | success | steps | findings |
|---|---|---|---|---:|---|
| `20261112-115416__ablation-g1-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-001` | `rollout-0` | False | 40 | - |
| `20261112-115416__ablation-g1-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-003` | `rollout-0` | False | 80 | - |
| `20261112-115416__ablation-g1-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-004` | `rollout-0` | False | 61 | - |
| `20261112-115416__ablation-g1-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-005` | `rollout-0` | True | 46 | - |
| `20261112-115416__ablation-g1-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-006` | `rollout-0` | False | 69 | - |
| `20261112-115416__ablation-g1-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-007` | `rollout-0` | False | 103 | - |
| `20261112-115416__ablation-g1-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-008` | `rollout-0` | True | 87 | - |
| `20261112-115416__ablation-g1-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-012` | `rollout-0` | False | 64 | - |
| `20261112-115649__ablation-g1-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-001` | `rollout-0` | False | 38 | - |
| `20261112-115649__ablation-g1-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-003` | `rollout-0` | False | 96 | - |
| `20261112-115649__ablation-g1-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-004` | `rollout-0` | False | 109 | - |
| `20261112-115649__ablation-g1-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-005` | `rollout-0` | True | 125 | - |
| `20261112-115649__ablation-g1-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-006` | `rollout-0` | False | 113 | - |
| `20261112-115649__ablation-g1-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-007` | `rollout-0` | True | 59 | - |
| `20261112-115649__ablation-g1-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-008` | `rollout-0` | True | 78 | - |
| `20261112-115649__ablation-g1-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-7ee30339` | `evo-f881cb75-012` | `rollout-0` | False | 100 | repeated_command@12-100, zero_edit_stall@1-100 |
| `20261112-115650__ablation-g0-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-001` | `rollout-0` | False | 40 | - |
| `20261112-115650__ablation-g0-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-003` | `rollout-0` | False | 119 | - |
| `20261112-115650__ablation-g0-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-004` | `rollout-0` | False | 84 | - |
| `20261112-115650__ablation-g0-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-005` | `rollout-0` | True | 67 | - |
| `20261112-115650__ablation-g0-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-006` | `rollout-0` | False | 100 | repeated_command@56-100, zero_edit_stall@1-100 |
| `20261112-115650__ablation-g0-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-007` | `rollout-0` | False | 100 | - |
| `20261112-115650__ablation-g0-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-008` | `rollout-0` | False | 100 | repeated_command@22-100, zero_edit_stall@1-100 |
| `20261112-115650__ablation-g0-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-012` | `rollout-0` | False | 28 | - |
| `20261112-115651__ablation-g0-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-001` | `rollout-0` | False | 58 | - |
| `20261112-115651__ablation-g0-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-003` | `rollout-0` | False | 112 | - |
| `20261112-115651__ablation-g0-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-004` | `rollout-0` | False | 122 | - |
| `20261112-115651__ablation-g0-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-005` | `rollout-0` | True | 78 | - |
| `20261112-115651__ablation-g0-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-006` | `rollout-0` | False | 100 | - |
| `20261112-115651__ablation-g0-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-007` | `rollout-0` | False | 100 | repeated_command@29-100 |
| `20261112-115651__ablation-g0-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-008` | `rollout-0` | False | 43 | - |
| `20261112-115651__ablation-g0-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-bb3f1cd9` | `evo-f881cb75-012` | `rollout-0` | False | 100 | repeated_command@19-100 |
| `20261112-115651__ablation-g2-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-001` | `rollout-0` | False | 36 | - |
| `20261112-115651__ablation-g2-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-003` | `rollout-0` | False | 64 | - |
| `20261112-115651__ablation-g2-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-004` | `rollout-0` | False | 100 | repeated_command@20-25, repeated_command@29-100 |
| `20261112-115651__ablation-g2-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-005` | `rollout-0` | True | 24 | - |
| `20261112-115651__ablation-g2-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-006` | `rollout-0` | False | 100 | repeated_command@31-100, zero_edit_stall@1-100 |
| `20261112-115651__ablation-g2-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-007` | `rollout-0` | True | 24 | - |
| `20261112-115651__ablation-g2-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-008` | `rollout-0` | True | 46 | - |
| `20261112-115651__ablation-g2-rep1__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-012` | `rollout-0` | False | 55 | - |
| `20261112-115653__ablation-g2-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-001` | `rollout-0` | False | 38 | - |
| `20261112-115653__ablation-g2-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-003` | `rollout-0` | False | 117 | - |
| `20261112-115653__ablation-g2-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-004` | `rollout-0` | False | 134 | repeated_command@43-50, repeated_command@59-99 |
| `20261112-115653__ablation-g2-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-005` | `rollout-0` | True | 130 | repeated_command@84-96, repeated_command@86-98, repeated_command@88-100 |
| `20261112-115653__ablation-g2-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-006` | `rollout-0` | False | 132 | repeated_command@53-57, repeated_command@66-100 |
| `20261112-115653__ablation-g2-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-007` | `rollout-0` | True | 83 | - |
| `20261112-115653__ablation-g2-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-008` | `rollout-0` | True | 61 | - |
| `20261112-115653__ablation-g2-rep2__swe-fixed__deepseek-v4.1-flash__8tasks__v-a2385770` | `evo-f881cb75-012` | `rollout-0` | False | 79 | - |
| `20261110-152609__monitored__7865cf` | `evo-f881cb75-000` | `rollout-0` | False | 51 | - |
| `20261110-152609__monitored__7865cf` | `evo-f881cb75-001` | `rollout-0` | False | 67 | - |
| `20261110-152609__monitored__7865cf` | `evo-f881cb75-002` | `rollout-0` | True | 14 | - |
| `20261110-152609__monitored__7865cf` | `evo-f881cb75-003` | `rollout-0` | False | 104 | - |
| `20261110-152609__monitored__7865cf` | `evo-f881cb75-004` | `rollout-0` | False | 120 | - |
| `20261110-152609__monitored__7865cf` | `evo-f881cb75-005` | `rollout-0` | False | 106 | - |
| `20261110-152609__monitored__7865cf` | `evo-f881cb75-006` | `rollout-0` | False | 100 | repeated_command@37-100 |
| `20261110-152609__monitored__7865cf` | `evo-f881cb75-007` | `rollout-0` | False | 92 | - |
| `20261110-152609__monitored__7865cf` | `evo-f881cb75-008` | `rollout-0` | False | 76 | - |
| `20261110-152609__monitored__7865cf` | `evo-f881cb75-009` | `rollout-0` | True | 26 | - |
| `20261110-152609__monitored__7865cf` | `evo-f881cb75-010` | `rollout-0` | True | 26 | - |
| `20261110-152609__monitored__7865cf` | `evo-f881cb75-011` | `rollout-0` | True | 10 | - |
| `20261110-152609__monitored__7865cf` | `evo-f881cb75-012` | `rollout-0` | False | 18 | - |
| `20261110-161536__monitored__a2d7df` | `evo-f881cb75-003` | `rollout-0` | False | 81 | - |
| `20261110-161536__monitored__a2d7df` | `evo-f881cb75-004` | `rollout-0` | False | 121 | - |
| `20261110-161536__monitored__a2d7df` | `evo-f881cb75-006` | `rollout-0` | False | 100 | - |
| `20261110-161536__monitored__a2d7df` | `evo-f881cb75-012` | `rollout-0` | False | 17 | - |
| `20261110-191001__click4-wide-conflict-reu__swe-fixed__deepseek-v4.1-flash__4tasks__v-7095e846` | `evo-f881cb75-000` | `rollout-0` | False | 43 | - |
| `20261110-191001__click4-wide-conflict-reu__swe-fixed__deepseek-v4.1-flash__4tasks__v-7095e846` | `evo-f881cb75-005` | `rollout-0` | True | 49 | - |
| `20261110-191001__click4-wide-conflict-reu__swe-fixed__deepseek-v4.1-flash__4tasks__v-7095e846` | `evo-f881cb75-007` | `rollout-0` | False | 45 | - |
| `20261110-191001__click4-wide-conflict-reu__swe-fixed__deepseek-v4.1-flash__4tasks__v-7095e846` | `evo-f881cb75-008` | `rollout-0` | True | 102 | - |
| `20261110-202219__click2-conflict-guard__swe-fixed__deepseek-v4.1-flash__2tasks__v-c018dbf1` | `evo-f881cb75-000` | `rollout-0` | False | 22 | - |
| `20261110-202219__click2-conflict-guard__swe-fixed__deepseek-v4.1-flash__2tasks__v-c018dbf1` | `evo-f881cb75-007` | `rollout-0` | True | 104 | - |
| `20261110-224455__click000-conflict-guard-__swe-fixed__deepseek-v4.1-flash__1tasks__v-782bc7e3` | `evo-f881cb75-000` | `rollout-0` | False | 17 | - |
| `20261110-224917__click15-failures-except-__swe-fixed__deepseek-v4.1-flash__8tasks__v-19d7d391` | `evo-f881cb75-001` | `rollout-0` | False | 16 | - |
| `20261110-224917__click15-failures-except-__swe-fixed__deepseek-v4.1-flash__8tasks__v-19d7d391` | `evo-f881cb75-003` | `rollout-0` | False | 59 | - |
| `20261110-224917__click15-failures-except-__swe-fixed__deepseek-v4.1-flash__8tasks__v-19d7d391` | `evo-f881cb75-004` | `rollout-0` | False | 142 | repeated_command@36-100 |
| `20261110-224917__click15-failures-except-__swe-fixed__deepseek-v4.1-flash__8tasks__v-19d7d391` | `evo-f881cb75-005` | `rollout-0` | False | 98 | - |
| `20261110-224917__click15-failures-except-__swe-fixed__deepseek-v4.1-flash__8tasks__v-19d7d391` | `evo-f881cb75-006` | `rollout-0` | False | 109 | - |
| `20261110-224917__click15-failures-except-__swe-fixed__deepseek-v4.1-flash__8tasks__v-19d7d391` | `evo-f881cb75-007` | `rollout-0` | False | 100 | repeated_command@28-100 |
| `20261110-224917__click15-failures-except-__swe-fixed__deepseek-v4.1-flash__8tasks__v-19d7d391` | `evo-f881cb75-008` | `rollout-0` | True | 90 | - |
| `20261110-224917__click15-failures-except-__swe-fixed__deepseek-v4.1-flash__8tasks__v-19d7d391` | `evo-f881cb75-012` | `rollout-0` | False | 11 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-000` | `rollout-0` | False | 18 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-000` | `rollout-1` | False | 36 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-000` | `rollout-2` | False | 66 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-000` | `rollout-3` | False | 27 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-000` | `rollout-4` | True | 35 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-001` | `rollout-0` | False | 201 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-001` | `rollout-1` | False | 206 | repeated_command@179-194 |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-001` | `rollout-2` | False | 227 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-001` | `rollout-3` | False | 197 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-001` | `rollout-4` | False | 62 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-002` | `rollout-0` | False | 137 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-002` | `rollout-1` | False | 113 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-002` | `rollout-2` | False | 133 | repeated_command@106-133, repeated_command@107-132 |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-002` | `rollout-3` | False | 113 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-002` | `rollout-4` | False | 119 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-003` | `rollout-0` | False | 39 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-003` | `rollout-1` | False | 74 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-003` | `rollout-2` | False | 63 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-003` | `rollout-3` | False | 200 | repeated_command@32-116, repeated_command@30-87, repeated_command@42-118, repeated_command@105-141, repeated_command@143-199 |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-003` | `rollout-4` | False | 62 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-004` | `rollout-0` | False | 113 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-004` | `rollout-1` | False | 185 | repeated_command@96-114 |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-004` | `rollout-2` | False | 241 | repeated_command@49-92, repeated_command@50-91, repeated_command@52-90 |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-004` | `rollout-3` | False | 40 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-004` | `rollout-4` | False | 46 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-005` | `rollout-0` | False | 61 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-005` | `rollout-1` | True | 106 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-005` | `rollout-2` | True | 47 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-005` | `rollout-3` | True | 76 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-005` | `rollout-4` | False | 91 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-006` | `rollout-0` | False | 105 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-006` | `rollout-1` | False | 114 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-006` | `rollout-2` | False | 107 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-006` | `rollout-3` | False | 102 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-006` | `rollout-4` | False | 180 | repeated_command@39-48, repeated_command@49-177 |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-007` | `rollout-0` | False | 40 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-007` | `rollout-1` | False | 41 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-007` | `rollout-2` | True | 200 | repeated_command@127-130, repeated_command@196-200 |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-007` | `rollout-3` | False | 46 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-007` | `rollout-4` | False | 201 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-008` | `rollout-0` | True | 47 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-008` | `rollout-1` | True | 77 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-008` | `rollout-2` | True | 47 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-008` | `rollout-3` | True | 73 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-008` | `rollout-4` | False | 46 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-009` | `rollout-0` | True | 68 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-009` | `rollout-1` | True | 128 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-009` | `rollout-2` | True | 45 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-009` | `rollout-3` | True | 108 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-009` | `rollout-4` | True | 72 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-010` | `rollout-0` | True | 218 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-010` | `rollout-1` | True | 195 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-010` | `rollout-2` | True | 75 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-010` | `rollout-3` | True | 263 | - |
| `20261106-000907__monitored__5eb57b` | `evo-f881cb75-010` | `rollout-4` | True | 153 | repeated_command@119-131 |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-000` | `rollout-0` | False | 87 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-000` | `rollout-1` | False | 28 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-000` | `rollout-2` | False | 34 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-000` | `rollout-3` | False | 37 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-000` | `rollout-4` | False | 42 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-001` | `rollout-0` | False | 118 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-001` | `rollout-1` | False | 96 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-001` | `rollout-2` | False | 135 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-001` | `rollout-3` | False | 136 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-001` | `rollout-4` | False | 241 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-002` | `rollout-0` | False | 139 | repeated_command@49-139 |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-002` | `rollout-1` | False | 128 | repeated_command@91-126 |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-002` | `rollout-2` | False | 141 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-002` | `rollout-3` | False | 136 | repeated_command@127-136 |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-002` | `rollout-4` | False | 146 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-003` | `rollout-0` | False | 87 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-003` | `rollout-1` | False | 100 | repeated_command@67-75 |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-003` | `rollout-2` | False | 177 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-003` | `rollout-3` | False | 65 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-003` | `rollout-4` | False | 35 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-004` | `rollout-0` | False | 61 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-004` | `rollout-1` | False | 45 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-004` | `rollout-2` | False | 188 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-004` | `rollout-3` | False | 46 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-004` | `rollout-4` | False | 53 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-005` | `rollout-0` | True | 39 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-005` | `rollout-1` | False | 33 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-005` | `rollout-2` | False | 49 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-005` | `rollout-3` | False | 63 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-005` | `rollout-4` | True | 66 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-006` | `rollout-0` | False | 112 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-006` | `rollout-1` | False | 121 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-006` | `rollout-2` | False | 128 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-006` | `rollout-3` | False | 124 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-006` | `rollout-4` | False | 93 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-007` | `rollout-0` | False | 68 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-007` | `rollout-1` | False | 51 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-007` | `rollout-2` | False | 37 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-007` | `rollout-3` | False | 62 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-007` | `rollout-4` | True | 267 | repeated_command@62-133 |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-008` | `rollout-0` | False | 107 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-008` | `rollout-1` | True | 173 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-008` | `rollout-2` | False | 276 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-008` | `rollout-3` | False | 275 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-008` | `rollout-4` | False | 115 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-009` | `rollout-0` | True | 38 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-009` | `rollout-1` | True | 51 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-009` | `rollout-2` | True | 40 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-009` | `rollout-3` | True | 38 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-009` | `rollout-4` | True | 49 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-010` | `rollout-0` | True | 51 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-010` | `rollout-1` | True | 157 | repeated_command@115-127, repeated_command@129-148, repeated_command@144-150 |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-010` | `rollout-2` | True | 84 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-010` | `rollout-3` | False | 144 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-010` | `rollout-4` | True | 126 | repeated_command@121-124 |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-011` | `rollout-0` | True | 62 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-011` | `rollout-1` | True | 12 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-011` | `rollout-2` | True | 28 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-011` | `rollout-3` | True | 16 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-011` | `rollout-4` | False | 200 | repeated_command@34-198 |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-012` | `rollout-0` | False | 247 | repeated_command@81-83, repeated_command@100-152 |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-012` | `rollout-1` | False | 37 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-012` | `rollout-2` | False | 88 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-012` | `rollout-3` | False | 17 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-012` | `rollout-4` | False | 69 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-013` | `rollout-0` | False | 80 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-013` | `rollout-1` | False | 211 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-013` | `rollout-2` | False | 96 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-013` | `rollout-3` | False | 110 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-013` | `rollout-4` | False | 116 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-014` | `rollout-0` | False | 41 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-014` | `rollout-1` | False | 52 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-014` | `rollout-2` | False | 204 | repeated_command@78-80, repeated_command@85-87, repeated_command@92-94, repeated_command@144-146, repeated_command@147-152, repeated_command@156-162, repeated_command@176-195 |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-014` | `rollout-3` | False | 163 | - |
| `20261108-195223__monitored__805ad7` | `evo-f881cb75-014` | `rollout-4` | False | 285 | - |

## Skipped

- {'run': '20261110-161443__monitored__f266b9', 'reason': 'no rollout directories'}
- {'run': '20261110-190921__click4-wide-conflict-reu__swe-fixed__deepseek-v4.1-flash__4tasks__v-7095e846', 'reason': 'no rollout directories'}

## 建议阈值

K1=3：温和提示 + 失败轨迹挖掘，跨模型通用。
硬停按模型分：qwen3.8-flash H=20，deepseek-v4.1-flash H=8；未知模型不硬停，先 shadow 校准。
H 的语义是第 H 次重复时跳过该条命令的沙盒执行并返回拒绝观察，不终止 rollout。
`805ad7` / `evo-f881cb75-007` / rollout-4（72 次 `inspect.getsource`，占 72/133 步后侥幸成功）归类为硬停预期会干预的病态轨迹，不受阈值保护。旧结论 H=73 作废，原因是被该异常值拖高。
K2=K3=3 维持，未经校准（语料中无连续截断或格式失败）。

### 按模型拆分的最长链

每条成功或失败轨迹取「同一命令 + 同一 sha + 中间无编辑」的最长链。qwen 为 `5eb57b` 与 `805ad7`，deepseek 为其余 run。表中的成功最长链是绝对值；定 H 时去掉 `805ad7/007/rollout-4` 那条病态成功，qwen 为 17，deepseek 为 3。

| 模型 | 成功条数 | 成功最长链 | 失败条数 | 失败最长链 |
|---|---:|---:|---:|---:|
| qwen3.8-flash | 36 | 72 | 94 | 163 |
| deepseek-v4.1-flash | 21 | 3 | 59 | 89 |

成功轨迹最长链直方图（链长 → 条数）：

- qwen3.8-flash：1→30, 2→1, 4→1, 5→1, 13→1, 17→1, 72→1
- deepseek-v4.1-flash：1→20, 3→1

qwen 成功轨迹里长于 1 的链：

- 72 次 `20261108-195223__monitored__805ad7` / `evo-f881cb75-007` / rollout-4
- 17 次 `20261108-195223__monitored__805ad7` / `evo-f881cb75-010` / rollout-1
- 13 次 `20261106-000907__monitored__5eb57b` / `evo-f881cb75-010` / rollout-4
- 5 次 `20261106-000907__monitored__5eb57b` / `evo-f881cb75-007` / rollout-2
- 4 次 `20261108-195223__monitored__805ad7` / `evo-f881cb75-010` / rollout-4
- 2 次 `20261106-000907__monitored__5eb57b` / `evo-f881cb75-009` / rollout-4

谓词补上 `open(..., "w").write(` 之后，`5eb57b` / `evo-f881cb75-010` / rollout-3 的 15 次 `nl | sed` 不再成链。其余原先判为真实空转的成功标记还在，包括 17 次 awk 重读和 72 次 `inspect.getsource`。
