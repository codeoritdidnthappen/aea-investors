<?php

namespace AeaiPortalChat\Service;

use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Services\PatientService;
use Symfony\Component\HttpFoundation\JsonResponse;

/**
 * Writes the caller's own confirmed name/DOB/address to their patient chart (TICK-016,
 * TICK-042). Delegates to `PatientService::update()`
 * (`src/Services/PatientService.php:307`) -- real, callable OpenEMR business logic, the
 * same class of call `AppointmentBookService` (TICK-040) already uses for booking. This
 * exists because the Standard API route `PUT /api/patient/:puuid`
 * (`ai_server/openemr/demographics.py`'s pre-TICK-042 target) is gated by a staff ACL
 * check (`RestConfig::request_authorization_check($request, "patients", "demo")` ->
 * `AclMain::aclCheckCore()` against a logged-in staff `authUser`), never an OAuth
 * scope -- structurally unreachable for a genuine patient-context bearer token, the
 * identical gap TICK-040 already root-caused and fixed for booking (see
 * `AppointmentBookService`'s own doc comment and
 * `tickets/TICK-042-fix-demographics-write-unreachable.md`). This module route is
 * enforced by `AuthorizationListener`'s OAuth-scope check instead, the same mechanism
 * `AppointmentBookController`/`AppointmentCancelController`/`AssessmentDraftController`
 * already use successfully.
 *
 * `$patientUuid` must always come from `HttpRestRequest::getPatientUUIDString()`
 * (token-derived); this class does not validate that itself, the caller owns that
 * invariant (same contract as `AppointmentBookService`). There is no path here that can
 * write demographics for a different patient than the one the bearer token actually
 * authenticated -- `PatientService::update()` itself resolves the target row from this
 * same uuid string.
 */
class PatientDemographicsUpdateService
{
    /**
     * The `patient_data` columns a patient may write about themselves, by their real
     * OpenEMR column names. Every one is optional (TICK-049): `PatientService::update()`
     * builds its `UPDATE` from the keys actually passed
     * (`BaseService::buildUpdateColumns`) and `PatientValidator`'s
     * DATABASE_UPDATE_CONTEXT requires only `uuid`, so omitting a field leaves the
     * stored value alone rather than blanking it. That is what makes an address-only
     * write possible without re-confirming a name and date of birth that are not
     * changing. Address components are separate columns here rather than one
     * concatenated `street` line, so what is stored matches what the patient confirmed.
     *
     * Anything outside this list is refused, not dropped: a request that asked to write
     * a field this endpoint cannot write must not come back `200 updated`.
     */
    private const WRITABLE_STRING_FIELDS = [
        'fname',
        'lname',
        'DOB',
        'street',
        'street_line_2',
        'city',
        'state',
        'postal_code',
    ];

    /**
     * The only field that may be sent empty, meaning "clear it". An address is written
     * as a whole unit, so a patient moving to an address with no apartment/unit line
     * must be able to clear the previous one -- otherwise the stored address is a
     * blend of the old and new. Every other field here has no such "not applicable"
     * state, and an empty value for one of them can only be a mistake, so it stays a
     * 400 rather than silently blanking a name or a date of birth.
     */
    private const CLEARABLE_STRING_FIELDS = ['street_line_2'];

    public function update(?string $patientUuid, array $body): JsonResponse
    {
        if (empty($patientUuid) || !UuidRegistry::isValidStringUUID($patientUuid)) {
            return $this->errorResponse(401, 'no bound patient on this request');
        }

        $fields = $this->validatedFields($body);
        if ($fields instanceof JsonResponse) {
            return $fields;
        }

        $result = (new PatientService())->update($patientUuid, $fields);
        if (!$result->isValid()) {
            return $this->errorResponse(400, 'validation failed', $result->getValidationMessages());
        }
        if (!empty($result->getInternalErrors())) {
            return $this->errorResponse(500, 'OpenEMR could not update the patient record');
        }

        return new JsonResponse(['status' => 'updated'], 200);
    }

    /**
     * @return array<string,string>|JsonResponse
     */
    private function validatedFields(array $body): array|JsonResponse
    {
        $errors = [];
        $fields = [];

        $unwritable = 0;

        foreach ($body as $field => $value) {
            if (!in_array($field, self::WRITABLE_STRING_FIELDS, true)) {
                // Counted, not echoed. Reflecting each unrecognised key back would let a
                // caller size the error body with its own strings, one entry per key.
                // The writable list below is all a legitimate caller needs.
                $unwritable++;
                continue;
            }
            if (!is_string($value)) {
                $errors[] = "$field must be a string";
                continue;
            }
            if ($value === '' && !in_array($field, self::CLEARABLE_STRING_FIELDS, true)) {
                $errors[] = "$field must be a non-empty string";
                continue;
            }
            $fields[$field] = $value;
        }

        if ($unwritable > 0) {
            $errors[] = $unwritable . ' field(s) in the request are not writable '
                . 'demographics fields; writable fields are: '
                . implode(', ', self::WRITABLE_STRING_FIELDS);
        }

        if (!empty($errors)) {
            return $this->errorResponse(400, 'validation failed', $errors);
        }

        // A body with nothing recognised in it must not report success having written
        // nothing; a partial write is allowed, an empty one is not.
        if (empty($fields)) {
            return $this->errorResponse(400, 'validation failed', [
                'at least one demographics field is required: '
                    . implode(', ', self::WRITABLE_STRING_FIELDS),
            ]);
        }

        return $fields;
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
