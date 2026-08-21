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
    private const REQUIRED_STRING_FIELDS = ['fname', 'DOB', 'street'];

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

        foreach (self::REQUIRED_STRING_FIELDS as $field) {
            $value = $body[$field] ?? null;
            if (!is_string($value) || $value === '') {
                $errors[] = "$field must be a non-empty string";
                continue;
            }
            $fields[$field] = $value;
        }

        // `lname` alone may be a confirmed empty string (a mononym) -- the same
        // allowance `ai_server/openemr/demographics.py`'s own `ConfirmedIdentity`
        // makes; every other field above must be non-empty.
        $lname = $body['lname'] ?? null;
        if (!is_string($lname)) {
            $errors[] = 'lname must be a string';
        } else {
            $fields['lname'] = $lname;
        }

        if (!empty($errors)) {
            return $this->errorResponse(400, 'validation failed', $errors);
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
