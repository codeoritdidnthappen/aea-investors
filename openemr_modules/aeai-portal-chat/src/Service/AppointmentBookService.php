<?php

namespace AeaiPortalChat\Service;

use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Services\AppointmentService;
use OpenEMR\Services\PatientService;
use Symfony\Component\HttpFoundation\JsonResponse;

/**
 * Books a new appointment for the caller's own bound patient (TICK-040).
 * Delegates to `AppointmentService::insert()` (`src/Services/AppointmentService.php:311`)
 * -- real, callable OpenEMR business logic, the same class of call
 * `AppointmentCancelService` (TICK-031/036/041) already uses for cancellation. This
 * exists because the Standard API route `POST /api/patient/:pid/appointment`
 * (`OpenEmrBookingAdapter`'s pre-TICK-040 target) is gated by a staff ACL check
 * (`RestConfig::request_authorization_check()` -> `AclMain::aclCheckCore()` against
 * a logged-in staff `authUser`), never an OAuth scope -- structurally unreachable
 * for a genuine patient-context bearer token, confirmed by reading
 * `apis/routes/_rest_routes_standard.inc.php` and `RestConfig.php` directly (see
 * `tickets/TICK-040-add-portal-booking-route.md`). This module route is enforced by
 * `AuthorizationListener`'s OAuth-scope check instead, the same mechanism
 * `AppointmentCancelController`/`AssessmentDraftController` already use successfully.
 *
 * `$patientUuid` must always come from `HttpRestRequest::getPatientUUIDString()`
 * (token-derived); this class does not validate that itself, the caller owns that
 * invariant (same contract as `AppointmentCancelService`/`AssessmentDraftService`).
 * The numeric pid `AppointmentService::insert()` requires is resolved from that uuid
 * here, server-side, via `PatientService::getPidByUuid()` -- never accepted as
 * caller-supplied input, so there is no path here that can write an appointment for
 * a different patient than the one the bearer token actually authenticated.
 *
 * `PatientService::getPidByUuid()`/`getIdByUuid()` compares against a `binary(16)`
 * column with no string-to-bytes conversion of its own (the identical gap TICK-041
 * root-caused and fixed for `AppointmentCancelService::forPatient()`) -- `$patientUuid`
 * is converted with `UuidRegistry::uuidToBytes()` before that call for the same
 * reason.
 */
class AppointmentBookService
{
    // Every appointment this route creates starts in this status; the caller/model
    // supplies no policy of its own (FR-28: "the AI server defines no separate
    // scheduling policy or default"), matching pc_apptstatus='-' as OpenEMR's own
    // plain "scheduled, not yet confirmed" status -- the same value the pre-TICK-040
    // Standard API call already sent (`ai_server/scheduling/booking.py`'s
    // `_BOOKED_STATUS`).
    private const BOOKED_STATUS = '-';

    private const REQUIRED_INT_FIELDS = ['pc_catid', 'pc_duration', 'pc_facility', 'pc_billing_location'];
    private const REQUIRED_STRING_FIELDS = ['pc_title', 'pc_eventDate', 'pc_startTime'];

    public function book(?string $patientUuid, array $body): JsonResponse
    {
        if (empty($patientUuid) || !UuidRegistry::isValidStringUUID($patientUuid)) {
            return $this->errorResponse(401, 'no bound patient on this request');
        }
        $pid = (new PatientService())->getPidByUuid(UuidRegistry::uuidToBytes($patientUuid));
        if (empty($pid)) {
            return $this->errorResponse(401, 'no bound patient on this request');
        }

        $fields = $this->validatedFields($body);
        if ($fields instanceof JsonResponse) {
            return $fields;
        }

        $data = $fields;
        $data['pc_hometext'] = 'Booked by the AI scheduling assistant';
        $data['pc_apptstatus'] = self::BOOKED_STATUS;

        $eid = (new AppointmentService())->insert($pid, $data);
        if (empty($eid)) {
            return $this->errorResponse(500, 'OpenEMR could not create the appointment');
        }
        // insert() generates and writes a real uuid internally (a fresh
        // UuidRegistry::createUuid() call) but only returns $eid, not the uuid it
        // just used -- an extra SELECT is the only way to recover the exact value
        // actually stored, short of duplicating that internal generation, which
        // would produce a different uuid than the one on the row.
        $row = sqlQuery('SELECT uuid FROM openemr_postcalendar_events WHERE pc_eid = ?', [$eid]);
        $auuid = !empty($row['uuid']) ? UuidRegistry::uuidToString($row['uuid']) : (string) $eid;

        return new JsonResponse(['id' => $auuid, 'status' => 'booked'], 201);
    }

    /**
     * @return array<string,int|string>|JsonResponse
     */
    private function validatedFields(array $body): array|JsonResponse
    {
        $errors = [];
        $fields = [];

        foreach (self::REQUIRED_INT_FIELDS as $field) {
            $value = self::asInt($body[$field] ?? null);
            if ($value === null) {
                $errors[] = "$field must be an integer";
                continue;
            }
            $fields[$field] = $value;
        }

        foreach (self::REQUIRED_STRING_FIELDS as $field) {
            $value = $body[$field] ?? null;
            if (!is_string($value) || $value === '') {
                $errors[] = "$field must be a non-empty string";
                continue;
            }
            $fields[$field] = $value;
        }

        if (isset($body['pc_aid'])) {
            $value = self::asInt($body['pc_aid']);
            if ($value === null) {
                $errors[] = 'pc_aid must be an integer';
            } else {
                $fields['pc_aid'] = $value;
            }
        }

        if (!empty($errors)) {
            return $this->errorResponse(400, 'validation failed', $errors);
        }

        return $fields;
    }

    /** Accepts a real int or a digit-only string (JSON has no distinct integer type
     * guarantee for every client); anything else -- including a negative or
     * float-looking string -- is not a valid id/count here. */
    private static function asInt(mixed $value): ?int
    {
        if (is_int($value)) {
            return $value;
        }
        if (is_string($value) && ctype_digit($value)) {
            return (int) $value;
        }
        return null;
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
