<?php

namespace AeaiPortalChat\Service;

use OpenEMR\Common\Database\QueryUtils;
use Symfony\Component\HttpFoundation\JsonResponse;

/**
 * Persistence and field validation for the assessment draft (TICK-017), scoped to
 * the approved field contract (ONBOARDING_CONTRACT.md, "Field contract and flow
 * order", rows 6-9 -- identity/demographics are TICK-016's concern, not this one).
 *
 * Every method takes `$patientUuid` as its first argument and every query filters on
 * it (`WHERE patient_uuid = ?`) -- there is no code path here that can read or write
 * a row belonging to a different patient, regardless of what `$uuid` the caller
 * supplies. `$patientUuid` must always come from
 * `HttpRestRequest::getPatientUUIDString()` (token-derived); this class does not
 * validate that itself, the caller owns that invariant.
 */
class AssessmentDraftService
{
    private const CONTACT_METHODS = ['phone', 'email', 'portal_message'];
    private const HELP_TYPES = [
        'counseling_or_therapy',
        'psychiatric_evaluation_or_medication_support',
        'both',
        'not_sure_yet',
    ];
    private const VISIT_FORMATS = ['in_person', 'video', 'either', 'not_sure'];
    private const VISIT_TIME_WINDOWS = [
        'weekday_morning',
        'weekday_afternoon',
        'weekday_evening',
        'weekend',
        'no_preference',
    ];
    private const ACCOMMODATIONS = [
        'language_interpreter',
        'hearing_accommodation',
        'vision_accommodation',
        'mobility_accommodation',
        'other_accommodation',
    ];
    private const REQUIRED_FOR_COMPLETION = [
        'preferred_contact_method',
        'help_type',
        'visit_format',
        'visit_time_window',
    ];

    public function create(?string $patientUuid, array $body): JsonResponse
    {
        if (empty($patientUuid)) {
            return $this->errorResponse(401, 'no bound patient on this request');
        }
        $fields = $this->validatedFields($body, requireComplete: false);
        if ($fields instanceof JsonResponse) {
            return $fields;
        }
        $uuid = $this->newUuid();
        $now = gmdate('Y-m-d H:i:s');
        sqlInsert(
            "INSERT INTO aeai_assessment_draft
                (uuid, patient_uuid, status, payload, created_at, updated_at)
             VALUES (?, ?, 'draft', ?, ?, ?)",
            [$uuid, $patientUuid, json_encode($fields), $now, $now]
        );
        return new JsonResponse(['uuid' => $uuid, 'status' => 'draft', 'fields' => $fields], 201);
    }

    public function read(?string $patientUuid, string $uuid): JsonResponse
    {
        if (empty($patientUuid)) {
            return $this->errorResponse(401, 'no bound patient on this request');
        }
        $row = $this->forPatient($patientUuid, $uuid);
        if ($row === null) {
            return $this->errorResponse(404, 'no assessment draft with that id for this patient');
        }
        return new JsonResponse([
            'uuid' => $row['uuid'],
            'status' => $row['status'],
            'fields' => json_decode($row['payload'], true) ?? [],
        ], 200);
    }

    public function update(?string $patientUuid, string $uuid, array $body): JsonResponse
    {
        if (empty($patientUuid)) {
            return $this->errorResponse(401, 'no bound patient on this request');
        }
        $row = $this->forPatient($patientUuid, $uuid);
        if ($row === null) {
            return $this->errorResponse(404, 'no assessment draft with that id for this patient');
        }
        if ($row['status'] === 'completed') {
            return $this->errorResponse(409, 'this assessment is already completed and cannot be edited');
        }
        $requestedCompletion = ($body['status'] ?? null) === 'completed';
        $existing = json_decode($row['payload'], true) ?? [];
        // A checkpoint that changes preferred_contact_method without also resending
        // contact_value is a normal incremental update (ONBOARDING_CONTRACT.md's
        // "checkpoint that field" model), not a request to keep the old value under
        // the new method -- a phone number isn't a valid contact_value for
        // method=email. Drop the stale value so it isn't re-validated against a
        // method it was never entered for; the client re-supplies it in a later
        // checkpoint (required again before completion, since a missing
        // contact_value fails validation the same way an invalid one would).
        $newMethod = $body['preferred_contact_method'] ?? null;
        $methodChanging = $newMethod !== null && $newMethod !== ($existing['preferred_contact_method'] ?? null);
        if ($methodChanging && !array_key_exists('contact_value', $body)) {
            unset($existing['contact_value']);
        }
        $merged = array_merge($existing, $this->stripStatus($body));
        $fields = $this->validatedFields($merged, requireComplete: $requestedCompletion);
        if ($fields instanceof JsonResponse) {
            return $fields;
        }
        $status = $requestedCompletion ? 'completed' : 'draft';
        // Optimistic concurrency control: `AND version = ?` (the version read above)
        // closes the read-modify-write window entirely, not just the completion
        // case -- two concurrent checkpoint PUTs each merge from their own read, so
        // without this the second write to land would silently clobber the first
        // one's fields with its own (now-stale) merge. `status != 'completed'` is
        // now redundant with the version check (any concurrent write bumps the
        // version) but kept for a clearer error message on that specific case.
        sqlStatement(
            "UPDATE aeai_assessment_draft
                SET payload = ?, status = ?, updated_at = ?, version = version + 1
              WHERE patient_uuid = ? AND uuid = ? AND status != 'completed' AND version = ?",
            [json_encode($fields), $status, gmdate('Y-m-d H:i:s'), $patientUuid, $uuid, $row['version']]
        );
        if (QueryUtils::affectedRows() === 0) {
            // Something changed between the read above and this write -- find out
            // what, so the client gets an accurate, actionable error rather than a
            // generic one.
            $current = $this->forPatient($patientUuid, $uuid);
            if ($current !== null && $current['status'] === 'completed') {
                return $this->errorResponse(409, 'this assessment is already completed and cannot be edited');
            }
            return $this->errorResponse(
                409,
                'this assessment was changed by another request; reload and retry'
            );
        }
        return new JsonResponse(['uuid' => $uuid, 'status' => $status, 'fields' => $fields], 200);
    }

    /**
     * Returns the row only if it belongs to `$patientUuid` -- the entire binding
     * boundary is this WHERE clause. There is no query anywhere in this class that
     * looks a draft up by `uuid` alone.
     */
    private function forPatient(string $patientUuid, string $uuid): ?array
    {
        $row = sqlQuery(
            "SELECT uuid, status, payload, version FROM aeai_assessment_draft
              WHERE patient_uuid = ? AND uuid = ?",
            [$patientUuid, $uuid]
        );
        return $row ?: null;
    }

    private function stripStatus(array $body): array
    {
        unset($body['status']);
        return $body;
    }

    /**
     * @return array|JsonResponse Validated fields, or a 400 JsonResponse to return as-is.
     */
    private function validatedFields(array $body, bool $requireComplete): array|JsonResponse
    {
        $errors = [];
        $fields = [];

        if (array_key_exists('preferred_contact_method', $body)) {
            $method = $body['preferred_contact_method'];
            if (!in_array($method, self::CONTACT_METHODS, true)) {
                $errors[] = 'preferred_contact_method must be one of: ' . implode(', ', self::CONTACT_METHODS);
            } else {
                $fields['preferred_contact_method'] = $method;
                if ($method === 'phone') {
                    if (!array_key_exists('contact_value', $body)) {
                        $errors[] = 'contact_value is required (an E.164 US phone number) with method=phone';
                    } elseif (!preg_match('/^\+1\d{10}$/', (string)$body['contact_value'])) {
                        $errors[] = 'contact_value must be an E.164 US phone number for method=phone';
                    } else {
                        $fields['contact_value'] = (string)$body['contact_value'];
                    }
                } elseif ($method === 'email') {
                    if (!array_key_exists('contact_value', $body)) {
                        $errors[] = 'contact_value is required (a valid email address) with method=email';
                    } elseif (!filter_var((string)$body['contact_value'], FILTER_VALIDATE_EMAIL)) {
                        $errors[] = 'contact_value must be a valid email address for method=email';
                    } else {
                        $fields['contact_value'] = (string)$body['contact_value'];
                    }
                }
            }
        }

        if (array_key_exists('help_type', $body)) {
            if (!in_array($body['help_type'], self::HELP_TYPES, true)) {
                $errors[] = 'help_type must be one of: ' . implode(', ', self::HELP_TYPES);
            } else {
                $fields['help_type'] = $body['help_type'];
            }
        }

        if (array_key_exists('visit_format', $body)) {
            if (!in_array($body['visit_format'], self::VISIT_FORMATS, true)) {
                $errors[] = 'visit_format must be one of: ' . implode(', ', self::VISIT_FORMATS);
            } else {
                $fields['visit_format'] = $body['visit_format'];
            }
        }

        if (array_key_exists('visit_time_window', $body)) {
            if (!in_array($body['visit_time_window'], self::VISIT_TIME_WINDOWS, true)) {
                $errors[] = 'visit_time_window must be one of: ' . implode(', ', self::VISIT_TIME_WINDOWS);
            } else {
                $fields['visit_time_window'] = $body['visit_time_window'];
            }
        }

        if (array_key_exists('accommodations', $body)) {
            if (!is_array($body['accommodations'])) {
                $errors[] = 'accommodations must be an array';
            } else {
                $accommodations = $body['accommodations'];
                $invalid = array_diff($accommodations, self::ACCOMMODATIONS);
                if (!empty($invalid)) {
                    $errors[] = 'accommodations may only contain: ' . implode(', ', self::ACCOMMODATIONS);
                } else {
                    $fields['accommodations'] = array_values($accommodations);
                    // ONBOARDING_CONTRACT.md row 9: the detail is optional even when
                    // other_accommodation is selected -- only its length is bounded
                    // when the patient does choose to provide one.
                    if (in_array('other_accommodation', $accommodations, true) && array_key_exists('accommodation_detail', $body)) {
                        $detail = (string)$body['accommodation_detail'];
                        if (strlen($detail) > 200) {
                            $errors[] = 'accommodation_detail must be 200 characters or fewer';
                        } else {
                            $fields['accommodation_detail'] = $detail;
                        }
                    }
                }
            }
        }

        if ($requireComplete) {
            foreach (self::REQUIRED_FOR_COMPLETION as $required) {
                if (empty($fields[$required])) {
                    $errors[] = "$required is required to complete the assessment";
                }
            }
        }

        if (!empty($errors)) {
            return $this->errorResponse(400, 'validation failed', $errors);
        }
        return $fields;
    }

    private function newUuid(): string
    {
        $bytes = random_bytes(16);
        $bytes[6] = chr((ord($bytes[6]) & 0x0f) | 0x40);
        $bytes[8] = chr((ord($bytes[8]) & 0x3f) | 0x80);
        return vsprintf('%s%s-%s-%s-%s-%s%s%s', str_split(bin2hex($bytes), 4));
    }

    private function errorResponse(int $status, string $message, array $details = []): JsonResponse
    {
        $body = ['error' => $message];
        if (!empty($details)) {
            $body['details'] = $details;
        }
        return new JsonResponse($body, $status);
    }
}
