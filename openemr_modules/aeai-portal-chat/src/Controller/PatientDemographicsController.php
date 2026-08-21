<?php

namespace AeaiPortalChat\Controller;

use AeaiPortalChat\Service\PatientDemographicsUpdateService;
use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Events\RestApiExtend\RestApiCreateEvent;
use OpenEMR\Events\RestApiExtend\RestApiScopeEvent;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;
use Symfony\Component\HttpFoundation\JsonResponse;

/**
 * Adds the patient-writable "confirm my demographics" action (TICK-042) -- see
 * `PatientDemographicsUpdateService`'s own doc comment for why the Standard API route
 * (`PUT /api/patient/:puuid`) is structurally unreachable for a genuine patient token
 * and this module route exists instead. Mirrors `AppointmentBookController`'s own
 * registration pattern exactly: same `RestApiExtend` events, same Portal route family,
 * no core file touched.
 */
class PatientDemographicsController
{
    use ParsesJsonRequestBody;

    public function subscribeToEvents(EventDispatcherInterface $eventDispatcher): void
    {
        $eventDispatcher->addListener(RestApiScopeEvent::EVENT_TYPE_GET_SUPPORTED_SCOPES, $this->addScopes(...));
        $eventDispatcher->addListener(RestApiCreateEvent::EVENT_HANDLE, $this->addRoutes(...));
    }

    public function addScopes(RestApiScopeEvent $event): RestApiScopeEvent
    {
        $event->addScope('patient', 'demographics', 'u');
        return $event;
    }

    public function addRoutes(RestApiCreateEvent $event): RestApiCreateEvent
    {
        $event->addToPortalRouteMap(
            'PUT /portal/patient/demographics',
            function (HttpRestRequest $request) {
                $body = $this->parseJsonBody();
                if ($body instanceof JsonResponse) {
                    return $body;
                }
                return (new PatientDemographicsUpdateService())->update($request->getPatientUUIDString(), $body);
            }
        );
        return $event;
    }
}
