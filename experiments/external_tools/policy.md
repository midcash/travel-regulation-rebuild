policy_id: universal-business-travel-policy-v1
version: 1
kind: business_travel
applicability: all_users
effective_from: 2026-01-01
effective_to: open

## 规则

| rule_id | topic | value | unit | source_ref |
| --- | --- | --- | --- | --- |
| airport_exit_buffer | airport_exit_buffer | 45 | minutes | universal-business-travel-policy-v1:airport_exit_buffer |
| rail_exit_buffer | rail_exit_buffer | 20 | minutes | universal-business-travel-policy-v1:rail_exit_buffer |
| meeting_arrival_buffer | meeting_arrival_buffer | 30 | minutes | universal-business-travel-policy-v1:meeting_arrival_buffer |
| local_transport_risk_buffer | local_transport_risk_buffer | 20 | minutes | universal-business-travel-policy-v1:local_transport_risk_buffer |
| pre_meeting_lodging_max_nights | pre_meeting_lodging_max_nights | 1 | night | universal-business-travel-policy-v1:pre_meeting_lodging_max_nights |
| local_transport_allowed | local_transport_allowed | true | boolean | universal-business-travel-policy-v1:local_transport_allowed |
