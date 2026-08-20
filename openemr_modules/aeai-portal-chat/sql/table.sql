-- Assessment drafts (TICK-017). One row per in-progress or completed assessment.
-- `patient_uuid` is stored exactly as HttpRestRequest::getPatientUUIDString() returns
-- it (a string, token-derived, never client input) -- every query is scoped by it, so
-- this column is the entire binding boundary. No FHIR/uuid_registry linkage: this is
-- a module-owned table, not a core clinical resource.
CREATE TABLE IF NOT EXISTS aeai_assessment_draft (
    id INT AUTO_INCREMENT PRIMARY KEY,
    uuid CHAR(36) NOT NULL,
    patient_uuid VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'draft',
    payload MEDIUMTEXT NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE KEY uk_aeai_assessment_draft_uuid (uuid),
    INDEX idx_aeai_assessment_draft_patient_uuid (patient_uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
