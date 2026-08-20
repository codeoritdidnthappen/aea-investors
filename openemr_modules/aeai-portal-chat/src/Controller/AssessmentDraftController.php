<?php

namespace AeaiPortalChat\Controller;

use AeaiPortalChat\Service\AssessmentDraftService;
use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Events\RestApiExtend\RestApiCreateEvent;
use OpenEMR\Events\RestApiExtend\RestApiScopeEvent;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;
use Symfony\Component\HttpFoundation\JsonResponse;

/**
 * Adds the patient-writable "assessment draft" resource OpenEMR v8.3.0 has no native
 * route for (TICK-017; evidence/TICK-001/ENDPOINT_MATRIX.md, "Start/checkpoint
 * assessment draft" / "Complete structured assessment" rows). Registers a new Portal
 * API route and a new OAuth scope through OpenEMR's own extension events -- no core
 * file is modified, and the pinned openemr/openemr:8.3.0 image is untouched.
 *
 * Why Portal, not FHIR: OpenEMR's AuthorizationListener unconditionally rejects any
 * FHIR write from a patient-role token, independent of which route/module registers
 * it (`if ($restRequest->isFhir()) { if ($restRequest->isPatientWriteRequest() &&
 * ... === 'patient') { throw ... } }`, src/RestControllers/Subscriber/
 * AuthorizationListener.php) -- a deliberate, acknowledged-temporary core policy, not
 * a bug to route around. Portal API routes carry no such block; the 5 built-in ones
 * (apis/routes/_rest_routes_portal.inc.php) are patient-role read routes today, and
 * this adds two more that are patient-role write routes, using the exact same
 * mechanism.
 *
 * Binding: every handler scopes to `$request->getPatientUUIDString()`, which is set
 * only by BearerTokenAuthorizationStrategy from the validated bearer token (never
 * client input) -- the same trusted mechanism the 5 built-in Portal routes already
 * use, and the check TICK-028 found *missing* on the FHIR Patient write route. A
 * cross-patient request 404s (see AssessmentDraftService::forPatient): the row simply
 * isn't found in the caller's own scoped query, not "found but rejected" -- there is
 * no query path that can return another patient's row at all.
 */
class AssessmentDraftController
{
    public function subscribeToEvents(EventDispatcherInterface $eventDispatcher): void
    {
        $eventDispatcher->addListener(RestApiScopeEvent::EVENT_TYPE_GET_SUPPORTED_SCOPES, $this->addScopes(...));
        $eventDispatcher->addListener(RestApiCreateEvent::EVENT_HANDLE, $this->addRoutes(...));
    }

    public function addScopes(RestApiScopeEvent $event): RestApiScopeEvent
    {
        $event->addScope('patient', 'assessment', 'c');
        $event->addScope('patient', 'assessment', 'r');
        $event->addScope('patient', 'assessment', 'u');
        return $event;
    }

    public function addRoutes(RestApiCreateEvent $event): RestApiCreateEvent
    {
        $event->addToPortalRouteMap(
            'POST /portal/patient/assessment',
            function (HttpRestRequest $request) {
                $body = $this->parseJsonBody();
                if ($body instanceof JsonResponse) {
                    return $body;
                }
                return (new AssessmentDraftService())->create($request->getPatientUUIDString(), $body);
            }
        );
        $event->addToPortalRouteMap(
            'GET /portal/patient/assessment/:auuid',
            function ($auuid, HttpRestRequest $request) {
                return (new AssessmentDraftService())->read($request->getPatientUUIDString(), $auuid);
            }
        );
        $event->addToPortalRouteMap(
            'PUT /portal/patient/assessment/:auuid',
            function ($auuid, HttpRestRequest $request) {
                $body = $this->parseJsonBody();
                if ($body instanceof JsonResponse) {
                    return $body;
                }
                return (new AssessmentDraftService())->update($request->getPatientUUIDString(), $auuid, $body);
            }
        );
        return $event;
    }

    /**
     * @return array|JsonResponse Decoded body, or a 400 if it isn't valid JSON.
     *
     * HttpRestRequest::getRequestBodyJSON() calls ->getContents() on a raw PHP
     * resource in this OpenEMR version and fatals; every core route reads the body
     * this way instead (see e.g. apis/routes/_rest_routes_standard.inc.php). A
     * missing/empty body is treated as an empty object (nothing to validate against
     * requireComplete=false); anything present that fails to parse is a 400, not a
     * silently-dropped write.
     */
    private function parseJsonBody(): array|JsonResponse
    {
        $raw = file_get_contents('php://input');
        if ($raw === false || trim($raw) === '') {
            return [];
        }
        $decoded = json_decode($raw, true);
        if (json_last_error() !== JSON_ERROR_NONE || !is_array($decoded)) {
            return new JsonResponse(['error' => 'request body must be a JSON object'], 400);
        }
        return $decoded;
    }
}
