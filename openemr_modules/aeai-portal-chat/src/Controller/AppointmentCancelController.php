<?php

namespace AeaiPortalChat\Controller;

use AeaiPortalChat\Service\AppointmentCancelService;
use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Events\RestApiExtend\RestApiCreateEvent;
use OpenEMR\Events\RestApiExtend\RestApiScopeEvent;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;

/**
 * Adds the patient-writable "cancel my own appointment" action OpenEMR v8.3.0 has no
 * Standard/FHIR route for (TICK-031; evidence/TICK-001/ENDPOINT_MATRIX.md, "Cancel by
 * status, retain history" row). The only built-in appointment mutation route is
 * `DELETE /api/patient/{pid}/appointment/{eid}` (`AppointmentRestController::
 * delete()`), which physically deletes the record via `deleteAppointmentRecord()` --
 * incompatible with FR-14 and ARCHITECTURE.md ADR-4 (cancellation must retain
 * history, never delete). This registers a new Portal API route instead, through the
 * same `OpenEMR\Events\RestApiExtend\*` extension events `AssessmentDraftController`
 * (TICK-017) already uses for the same reason: no core file is modified, and the
 * pinned openemr/openemr:8.3.0 image stays untouched.
 *
 * Why Portal, not FHIR/Standard: adding a new HTTP verb to a core Standard API route
 * file would mean editing shipped OpenEMR source. The Portal namespace is already how
 * this module extends the API surface (`AssessmentDraftController`); it carries no
 * core write-only-FHIR restriction (see that controller's own doc comment for why).
 * The route name mirrors the built-in `GET /portal/patient/appointment/:auuid`
 * (`apis/routes/_rest_routes_portal.inc.php`) rather than inventing a new identifier
 * scheme: a patient client that already lists/reads its own appointments through that
 * route can cancel one with the exact same `auuid` it already has.
 *
 * Binding: `AppointmentCancelService::forPatient()` calls
 * `AppointmentService::search(['puuid' => ..., 'pc_uuid' => ...])` -- the identical
 * two-filter call the built-in `AppointmentRestController::getOneForPatient()` already
 * uses to scope a single appointment to its owning patient -- with `puuid` set only to
 * `$request->getPatientUUIDString()`, itself set only by
 * `BearerTokenAuthorizationStrategy` from the validated bearer token, never client
 * input. A cross-patient request 404s because the `AND`-combined search returns no row
 * at all, not because a row was found and then rejected: there is no query path here
 * that can return another patient's appointment.
 */
class AppointmentCancelController
{
    public function subscribeToEvents(EventDispatcherInterface $eventDispatcher): void
    {
        $eventDispatcher->addListener(RestApiScopeEvent::EVENT_TYPE_GET_SUPPORTED_SCOPES, $this->addScopes(...));
        $eventDispatcher->addListener(RestApiCreateEvent::EVENT_HANDLE, $this->addRoutes(...));
    }

    public function addScopes(RestApiScopeEvent $event): RestApiScopeEvent
    {
        $event->addScope('patient', 'appointment', 'u');
        return $event;
    }

    public function addRoutes(RestApiCreateEvent $event): RestApiCreateEvent
    {
        $event->addToPortalRouteMap(
            'PUT /portal/patient/appointment/:auuid',
            function ($auuid, HttpRestRequest $request) {
                return (new AppointmentCancelService())->cancel(
                    $request->getPatientUUIDString(),
                    $auuid,
                    $request->getRequestUserId()
                );
            }
        );
        return $event;
    }
}
