<?php

namespace AeaiPortalChat\Controller;

use OpenEMR\Events\PatientPortal\RenderEvent;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;
use Symfony\Component\EventDispatcher\GenericEvent;

/**
 * Renders the AI chat launch entry on the authenticated patient portal.
 *
 * RenderEvent::EVENT_SECTION_RENDER_POST and RenderEvent::EVENT_DASHBOARD_INJECT_CARD
 * both fire only from portal/home.php, which never renders for a logged-out visitor
 * (evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md), so this class needs no login check of
 * its own — it is only ever reached after OpenEMR has already authenticated the
 * patient.
 *
 * The iframe's only src is the AI server's own OAuth launch endpoint. Nothing here
 * calls an OpenEMR API/FHIR route, reads a bearer token, or reads a patient identifier
 * (FR-4): the response is a single static iframe tag, so the browser's only network
 * request from this entry is to that one AI-server origin.
 *
 * TICK-032: the chat panel is one more `.collapse` card in the portal's existing
 * `#cardgroup` accordion, and `renderDashboardTile()` adds the matching launcher tile
 * to the dashboard's `#inject_card` row -- the same pattern every other portal feature
 * already uses (e.g. "Medical Reports" / `#downloadcard` in
 * templates/portal/home.html.twig, whose nav tile comes from
 * templates/portal/partial/_nav_icon.html.twig). Previously this fired only
 * EVENT_SECTION_RENDER_POST with a bare, non-collapsible `<section>`, which had no
 * dashboard tile and required scrolling past the whole dashboard to find
 * (evidence/TICK-024/DESKTOP_E2E_EVIDENCE.md, finding 1).
 */
class PortalChatController
{
    private const DEFAULT_CHAT_LAUNCH_URL = 'https://chat.localhost/oauth/launch';
    private const CARD_ID = 'aeai-portal-chat';

    public function subscribeToEvents(EventDispatcherInterface $eventDispatcher): void
    {
        $eventDispatcher->addListener(RenderEvent::EVENT_DASHBOARD_INJECT_CARD, $this->renderDashboardTile(...));
        $eventDispatcher->addListener(RenderEvent::EVENT_SECTION_RENDER_POST, $this->render(...));
    }

    /**
     * The dashboard grid tile, matching templates/portal/partial/_nav_icon.html.twig's
     * markup so it looks and behaves like the Clinical Documents / Appointments / etc.
     * tiles beside it. Clicking it toggles the `.collapse` panel `render()` outputs
     * below via Bootstrap's own accordion behavior (`data-parent="#cardgroup"`) -- no
     * JavaScript of this module's own is involved.
     */
    public function renderDashboardTile(GenericEvent $event): void
    {
        echo '<a id="aeai-chat-go" class="col-lg-2 col-md-4 col-sm-6 col-6 card bg-light '
            . 'pl-sm-2 pr-sm-2 pl-0 pr-0 pt-2 pb-2 text-center text-decoration-none" '
            . 'data-toggle="collapse" data-parent="#cardgroup" aria-expanded="false" '
            . 'href="#' . self::CARD_ID . '" data-window-title="AI Chat">'
            . '<h1 class="card-image"><i class="fa fa-2x fa-comments text-dark"></i></h1>'
            . '<div class="card-body pl-1 pr-1 pl-sm-3 pr-sm-3">'
            . '<button class="btn btn-success d-block w-100 text-light">AI Chat</button>'
            . '</div></a>';
    }

    /**
     * The accordion panel the dashboard tile above reveals -- a `.collapse` card
     * sharing `#cardgroup` with every other portal feature's panel, so it opens and
     * closes the same way Appointments, Medical Reports, etc. already do.
     */
    public function render(GenericEvent $event): void
    {
        $url = getenv('AEAI_PORTAL_CHAT_URL') ?: self::DEFAULT_CHAT_LAUNCH_URL;
        $escapedUrl = htmlspecialchars($url, ENT_QUOTES, 'UTF-8');
        echo '<div id="' . self::CARD_ID . '" class="card collapse overflow-auto" data-parent="#cardgroup">'
            . '<header class="card-header p-1 bg-dark text-light h3">AI Chat</header>'
            . '<div class="card-body p-0">'
            . '<iframe title="AI Chat" data-aeai-portal-chat="1" '
            . 'src="' . $escapedUrl . '" '
            . 'style="width:100%;min-height:640px;border:0;"></iframe>'
            . '</div></div>';
    }
}
