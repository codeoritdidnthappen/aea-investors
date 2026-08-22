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
     *
     * TICK-054: the iframe ships with no `src`. A `.collapse` card is `display:none`
     * until it is opened, but a hidden iframe still loads, so a launch URL present in
     * `src` at render started an authorization for a panel the patient never opened --
     * and because that authorization needs a login the OAuth2 provider's own session
     * cannot supply, TICK-045's breakout script then navigated the top-level window
     * off the dashboard the patient had just signed in to (FR-32). The launch URL
     * therefore rides in `data-src` and is promoted to `src` by
     * `deferredLoadScript()` below, on the patient's own gesture.
     */
    public function render(GenericEvent $event): void
    {
        $url = getenv('AEAI_PORTAL_CHAT_URL') ?: self::DEFAULT_CHAT_LAUNCH_URL;
        $escapedUrl = htmlspecialchars($url, ENT_QUOTES, 'UTF-8');
        echo '<div id="' . self::CARD_ID . '" class="card collapse overflow-auto" data-parent="#cardgroup">'
            . '<header class="card-header p-1 bg-dark text-light h3">AI Chat</header>'
            . '<div class="card-body p-0">'
            . '<iframe title="AI Chat" data-aeai-portal-chat="1" '
            . 'data-src="' . $escapedUrl . '" '
            . 'style="width:100%;min-height:640px;border:0;"></iframe>'
            . '</div></div>'
            . $this->deferredLoadScript();
    }

    /**
     * Promotes the panel's `data-src` to `src` the first time the patient opens the
     * AI Chat tile, and never again.
     *
     * Three properties this has to hold, all of them load-bearing:
     *
     * 1. *Only on a patient gesture.* The trigger is a `click` on the tile anchor,
     *    which is what both a mouse press and a keyboard activation of a focused
     *    anchor produce, so one listener covers NFR-19's keyboard bar without a
     *    second key-handling path of its own.
     * 2. *Not on the portal's own restore.* portal/home.php persists the last panel
     *    the patient used and re-opens it with `$(whereto).collapse('show')` on every
     *    subsequent dashboard load. That is the portal re-opening the panel, not the
     *    patient opening it, so a `show` with no preceding tile gesture is cancelled
     *    and the portal's own dashboard card is opened in its place -- the patient
     *    lands on the tile grid, exactly as a patient who has never opened the chat
     *    does, and no authorization begins. Without this the reported bug would
     *    simply move to the second dashboard load; without the fallback, cancelling
     *    would leave every card in `#cardgroup` collapsed and the dashboard blank,
     *    with the AI Chat tile itself hidden inside it.
     * 3. *Exactly once.* The transcript lives only in the panel's DOM (NFR-33 forbids
     *    persisting it), so re-assigning `src` on a later open would silently discard
     *    the patient's whole conversation. Bootstrap's collapse only toggles
     *    `display`, so the guarded assignment is all that is needed for a collapse
     *    and re-open to keep the live frame.
     *
     * Nothing but the same env-configured, escaped launch URL already in the markup
     * ever reaches the DOM here: no token, no patient identifier, and no second
     * network origin (FR-4).
     */
    private function deferredLoadScript(): string
    {
        return '<script>(function () {'
            . 'var jq = window.jQuery;'
            . 'var tile = document.getElementById("aeai-chat-go");'
            . 'var panel = document.getElementById("' . self::CARD_ID . '");'
            . 'if (!jq || !tile || !panel) { return; }'
            . 'var frame = panel.querySelector("iframe[data-aeai-portal-chat]");'
            . 'if (!frame) { return; }'
            . 'var openedByPatient = false;'
            . 'tile.addEventListener("click", function () { openedByPatient = true; });'
            . 'jq(panel).on("show.bs.collapse", function (event) {'
            . 'if (!openedByPatient) {'
            . 'event.preventDefault();'
            . 'var dashboard = document.getElementById("quickstart-card");'
            . 'if (dashboard && !dashboard.classList.contains("show")) {'
            . 'jq(dashboard).collapse("show");'
            . '}'
            . 'return;'
            . '}'
            . 'if (!frame.getAttribute("src")) {'
            . 'frame.setAttribute("src", frame.getAttribute("data-src"));'
            . '}'
            . '});'
            . '}());</script>';
    }
}
