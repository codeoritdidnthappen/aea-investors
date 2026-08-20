"""Guided onboarding assessment: field validation, supportive-content triggers, and
native OpenEMR draft/completion checkpointing (TICK-017).

Nothing in this package sends a patient answer to the external model: it has no
import of `ai_server.llm` (`ai_server/tests/test_onboarding_flow.py` asserts this
statically). Its only outbound HTTP targets are the two OpenEMR APIs FR-26/FR-30
name: the Portal API for draft checkpoint/completion (`draft_client.py`) and the
Standard API for the confirmed-demographics write (`ai_server/openemr/
demographics.py`) -- never the external model.
"""
