<?php

namespace AeaiPortalChat\Service;

use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Services\AppointmentService;
use OpenEMR\Validators\ProcessingResult;
use Symfony\Component\HttpFoundation\JsonResponse;

/**
 * Cancels one of the caller's own OpenEMR appointments by status update (TICK-031),
 * never a delete. Delegates to `AppointmentService::updateAppointmentStatus()`
 * (`src/Services/AppointmentService.php:591`) -- real, callable OpenEMR business
 * logic, the same method the classic staff calendar UI's own cancel action calls, not
 * a thin SQL wrapper this module invented. `list_options` ('apptstat') status 'x'
 * ("x Canceled", `sql/database.sql`) is a real, distinct-from-DELETE status;
 * `FhirAppointmentService` maps both 'x' and '%' to FHIR status "cancelled", so a
 * cancelled appointment disappears from `OpenEmrScheduleAdapter.active_appointments`
 * reads (TICK-018) exactly like any other cancellation, with the record itself
 * retained (ARCHITECTURE.md ADR-4).
 *
 * Every method takes `$patientUuid` as its first argument and `forPatient()` is the
 * entire binding boundary: it scopes `AppointmentService::search()` to both the
 * caller's own `puuid` and the requested `auuid` in the same call, the identical
 * two-filter pattern the built-in `AppointmentRestController::getOneForPatient()`
 * already uses. There is no query path here that can return, and therefore no path
 * that can cancel, a different patient's appointment, regardless of what `$auuid` the
 * caller supplies. `$patientUuid` must always come from
 * `HttpRestRequest::getPatientUUIDString()` (token-derived); this class does not
 * validate that itself, the caller owns that invariant (same contract as
 * `AssessmentDraftService`).
 */
class AppointmentCancelService
{
    // list_options 'apptstat': 'x' = "x Canceled" (sql/database.sql). '%' ("Canceled <
    // 24h") is a distinct, equally valid cancelled-with-history code this module does
    // not use -- picking one fixed status keeps this patient-facing action from
    // depending on any additional cancellation-window policy this ticket was never
    // asked to define (ticket "Out of Scope": no AI-defined policy).
    private const CANCELLED_STATUS = 'x';

    public function cancel(?string $patientUuid, string $auuid, ?int $userId): JsonResponse
    {
        if (empty($patientUuid)) {
            return $this->errorResponse(401, 'no bound patient on this request');
        }
        $appointment = $this->forPatient($patientUuid, $auuid);
        if ($appointment === null) {
            return $this->errorResponse(404, 'no appointment with that id for this patient');
        }
        if ($appointment['pc_apptstatus'] === self::CANCELLED_STATUS) {
            return $this->errorResponse(409, 'this appointment is already cancelled');
        }
        (new AppointmentService())->updateAppointmentStatus(
            $appointment['pc_eid'],
            self::CANCELLED_STATUS,
            $userId ?? 0
        );
        return new JsonResponse(['id' => $auuid, 'status' => 'cancelled'], 200);
    }

    /**
     * Returns the appointment row only if it belongs to `$patientUuid` -- the entire
     * binding boundary is this dual-filtered search. There is no query anywhere in
     * this class that looks an appointment up by `$auuid` alone.
     *
     * TICK-041 root cause: `AppointmentService::search()`'s `puuid`/`pc_uuid` filters
     * (like the identical two-filter call `AppointmentRestController::getOneForPatient()`
     * itself makes) reach `FhirSearchWhereClauseBuilder::build()`/`StringSearchField`,
     * which binds an exact-match filter as `BINARY <column> = ?` against the caller's
     * raw value. `openemr_postcalendar_events.uuid` and `patient_data.uuid` are both
     * `binary(16)` columns, but `$patientUuid`/`$auuid` arrive as 36-character dashed
     * UUID strings (`UuidRegistry::uuidToString()`'s format) -- a `binary(16)` column
     * can never equal a 36-byte string, so this call always returned zero rows
     * regardless of whether the ids were genuinely correct. Confirmed live against the
     * real local database and the real `AppointmentService::search()` method (not just
     * the generated SQL in isolation): the dashed-string form returns 0 rows for
     * `pc_eid=7`'s own confirmed-correct uuid pair, and converting both values with
     * `UuidRegistry::uuidToBytes()` first -- the same conversion
     * `InsuranceRestController` already performs before every `puuid` search this
     * pinned release ships with -- returns exactly that row. An invalid/malformed uuid
     * string is treated the same as "not found" (`cancel()`'s existing 404), not a
     * server error.
     */
    private function forPatient(string $patientUuid, string $auuid): ?array
    {
        if (!UuidRegistry::isValidStringUUID($patientUuid) || !UuidRegistry::isValidStringUUID($auuid)) {
            return null;
        }
        $result = (new AppointmentService())->search([
            'puuid' => UuidRegistry::uuidToBytes($patientUuid),
            'pc_uuid' => UuidRegistry::uuidToBytes($auuid),
        ]);
        $data = ProcessingResult::extractDataArray($result);
        return $data[0] ?? null;
    }

    private function errorResponse(int $status, string $message): JsonResponse
    {
        return new JsonResponse(['error' => $message], $status);
    }
}
