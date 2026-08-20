<?php

namespace AeaiPortalChat\Controller;

use OpenEMR\Events\PatientPortal\RenderEvent;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;
use Symfony\Component\EventDispatcher\GenericEvent;

/**
 * Renders the AI chat launch entry on the authenticated patient portal.
 *
 * RenderEvent::EVENT_SECTION_RENDER_POST fires only from portal/home.php, which never
 * renders for a logged-out visitor (evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md), so this
 * class needs no login check of its own — it is only ever reached after OpenEMR has
 * already authenticated the patient.
 *
 * The iframe's only src is the AI server's own OAuth launch endpoint. Nothing here
 * calls an OpenEMR API/FHIR route, reads a bearer token, or reads a patient identifier
 * (FR-4): the response is a single static iframe tag, so the browser's only network
 * request from this entry is to that one AI-server origin.
 */
class PortalChatController
{
    private const DEFAULT_CHAT_LAUNCH_URL = 'https://chat.localhost/oauth/launch';

    public function subscribeToEvents(EventDispatcherInterface $eventDispatcher): void
    {
        $eventDispatcher->addListener(RenderEvent::EVENT_SECTION_RENDER_POST, $this->render(...));
    }

    public function render(GenericEvent $event): void
    {
        $url = getenv('AEAI_PORTAL_CHAT_URL') ?: self::DEFAULT_CHAT_LAUNCH_URL;
        $escapedUrl = htmlspecialchars($url, ENT_QUOTES, 'UTF-8');
        echo '<section id="aeai-portal-chat" class="aeai-portal-chat">'
            . '<iframe title="AI Chat" data-aeai-portal-chat="1" '
            . 'src="' . $escapedUrl . '" '
            . 'style="width:100%;min-height:640px;border:0;"></iframe>'
            . '</section>';
    }
}
