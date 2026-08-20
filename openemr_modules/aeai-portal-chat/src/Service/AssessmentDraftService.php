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
        $merged = array_merge(json_decode($row['payload'], true) ?? [], $this->stripStatus($body));
        $fields = $this->validatedFields($merged, requireComplete: $requestedCompletion);
        if ($fields instanceof JsonResponse) {
            return $fields;
        }
        $status = $requestedCompletion ? 'completed' : 'draft';
        // `AND status != 'completed'` makes this a compare-and-swap: closes the
        // window between the read above and this write where a concurrent request
        // (e.g. a retried client submission) could complete the same draft in
        // between. If that happens, 0 rows are affected here even though the read
        // saw a non-completed row -- report the same 409 the read-time check above
        // would have, rather than a false 200.
        sqlStatement(
            "UPDATE aeai_assessment_draft
                SET payload = ?, status = ?, updated_at = ?
              WHERE patient_uuid = ? AND uuid = ? AND status != 'completed'",
            [json_encode($fields), $status, gmdate('Y-m-d H:i:s'), $patientUuid, $uuid]
        );
        if (QueryUtils::affectedRows() === 0) {
            return $this->errorResponse(409, 'this assessment is already completed and cannot be edited');
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
            "SELECT uuid, status, payload FROM aeai_assessment_draft
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
                    $value = (string)($body['contact_value'] ?? '');
                    if (!preg_match('/^\+1\d{10}$/', $value)) {
                        $errors[] = 'contact_value must be an E.164 US phone number for method=phone';
                    } else {
                        $fields['contact_value'] = $value;
                    }
                } elseif ($method === 'email') {
                    $value = (string)($body['contact_value'] ?? '');
                    if (!filter_var($value, FILTER_VALIDATE_EMAIL)) {
                        $errors[] = 'contact_value must be a valid email address for method=email';
                    } else {
                        $fields['contact_value'] = $value;
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
            $accommodations = is_array($body['accommodations']) ? $body['accommodations'] : [];
            $invalid = array_diff($accommodations, self::ACCOMMODATIONS);
            if (!empty($invalid)) {
                $errors[] = 'accommodations may only contain: ' . implode(', ', self::ACCOMMODATIONS);
            } else {
                $fields['accommodations'] = array_values($accommodations);
                if (in_array('other_accommodation', $accommodations, true)) {
                    $detail = (string)($body['accommodation_detail'] ?? '');
                    if ($detail === '' || strlen($detail) > 200) {
                        $errors[] = 'accommodation_detail is required (1-200 chars) when other_accommodation is selected';
                    } else {
                        $fields['accommodation_detail'] = $detail;
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
