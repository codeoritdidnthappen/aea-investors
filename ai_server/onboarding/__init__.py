"""Guided onboarding assessment: field validation, supportive-content triggers, and
native OpenEMR draft/completion checkpointing (TICK-017).

Nothing in this package sends a patient answer to the external model: it has no
import of `ai_server.llm` and no outbound HTTP target other than the configured
OpenEMR Portal API (`ai_server/tests/test_onboarding_flow.py` asserts this
statically).
"""
