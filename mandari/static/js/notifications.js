/**
 * Benachrichtigungen in Work: Zahl im Seitentitel und ein Hinweis, sobald neue eintreffen.
 *
 * Fragt regelmäßig die ungelesenen Benachrichtigungen ab. Jede Benachrichtigung wird in einem Browser höchstens
 * einmal eingeblendet: Der Zeitpunkt der neuesten schon gezeigten steht im Speicher des Browsers, der Server liefert
 * nur neuere (`?since=`). Vorher erschien die neueste ungelesene bei jedem Seitenaufruf erneut. Die Einblendung
 * selbst übernimmt die gemeinsame Anzeige unten rechts (templates/partials/toasts.html, Ereignis `show-toast`);
 * eine eigene oben rechts verdeckte Glocke und Kopfzeile.
 */

(function() {
    'use strict';

    const CONFIG = {
        pollInterval: 10000,  // Abstand der Abfragen in Millisekunden
        limit: 5,             // so viele liefert der Server höchstens je Abfrage
    };

    let pollingInterval = null;
    let lastCount = 0;
    let orgSlug = null;
    let konto = '';
    // Stand, falls der Browser nichts speichern lässt; gilt dann nur für diese Seite
    let merker;

    function init() {
        const container = document.querySelector('[data-org-slug]');
        if (container) {
            orgSlug = container.dataset.orgSlug;
            konto = container.dataset.konto || '';
        } else {
            const match = window.location.pathname.match(/\/work\/([^\/]+)/);
            if (match) {
                orgSlug = match[1];
            }
        }
        if (!orgSlug) {
            return;
        }
        startPolling();
        document.addEventListener('visibilitychange', handleVisibilityChange);
    }

    function startPolling() {
        if (pollingInterval) return;
        pollingInterval = setInterval(poll, CONFIG.pollInterval);
        poll();
    }

    function schluessel() {
        return `mandari:hinweise-stand:${konto}:${orgSlug}`;
    }

    /**
     * Zeitpunkt der neuesten schon gezeigten Benachrichtigung. `undefined`: in diesem Browser noch nie gemerkt;
     * leer: gemerkt, aber es gab noch keine ungelesene.
     */
    function standLesen() {
        try {
            const gemerkt = window.localStorage.getItem(schluessel());
            if (gemerkt !== null) return gemerkt;
        } catch (e) {
            // Speicher gesperrt: der Merker dieser Seite gilt
        }
        return merker;
    }

    function standMerken(stand) {
        merker = stand;
        try {
            window.localStorage.setItem(schluessel(), stand);
        } catch (e) {
            // Speicher gesperrt oder voll: der Merker dieser Seite gilt
        }
    }

    async function poll() {
        const gemerkt = standLesen();
        const since = gemerkt ? `?since=${encodeURIComponent(gemerkt)}` : '';
        try {
            const response = await fetch(`/work/${orgSlug}/notifications/latest/${since}`);
            if (response.ok) {
                const data = await response.json();
                handleNotificationUpdate(data, gemerkt);
            }
        } catch (error) {
            console.error('Notifications: Polling error', error);
        }
    }

    /**
     * Was aus einer Antwort einzublenden ist und welcher Stand danach gilt. Die Antwort enthält ungelesene
     * Benachrichtigungen, die neueste zuerst, mit `gemerkt` nur solche nach diesem Zeitpunkt. Beim ersten Besuch in
     * einem Browser wird nichts nachgeholt: Die ungelesenen stehen in der Glocke.
     */
    function auswerten(notifications, gemerkt) {
        const liste = Array.isArray(notifications) ? notifications : [];
        const neueste = liste.length && typeof liste[0].created_at === 'string' ? liste[0].created_at : '';
        if (gemerkt === undefined) {
            return { zeigen: [], stand: neueste };
        }
        return { zeigen: liste, stand: neueste || gemerkt };
    }

    /** Inhalt der Einblendung: eine Benachrichtigung mit Titel und Ziel, mehrere als ein Hinweis auf die Übersicht */
    function hinweis(zeigen, uebersicht) {
        if (zeigen.length === 1) {
            const eine = zeigen[0];
            const text = eine.message || eine.title || 'Neue Benachrichtigung';
            return { message: text, titel: eine.message ? (eine.title || '') : '', link: eine.link || uebersicht, type: 'info' };
        }
        const anzahl = zeigen.length >= CONFIG.limit ? 'mehrere' : String(zeigen.length);
        return { message: `Sie haben ${anzahl} neue Benachrichtigungen.`, titel: '', link: uebersicht, type: 'info' };
    }

    function handleNotificationUpdate(data, gemerkt) {
        const count = Number(data.count) || 0;
        updateTitle(count);
        lastCount = count;

        const ergebnis = auswerten(data.notifications, gemerkt);
        if (ergebnis.stand !== gemerkt) {
            standMerken(ergebnis.stand);
        }
        if (ergebnis.zeigen.length) {
            window.dispatchEvent(new CustomEvent('show-toast', {
                detail: hinweis(ergebnis.zeigen, `/work/${orgSlug}/notifications/`),
            }));
        }

        window.dispatchEvent(new CustomEvent('notification:update', {
            detail: { count, notifications: data.notifications },
        }));
    }

    /** Zahl der ungelesenen Benachrichtigungen vor dem Seitentitel, etwa „(3) Aufgaben“ */
    function updateTitle(count) {
        const ohne = document.title.replace(/^\(\d+\+?\)\s*/, '');
        document.title = count > 0 ? `(${count > 99 ? '99+' : count}) ${ohne}` : ohne;
    }

    function handleVisibilityChange() {
        if (document.visibilityState === 'visible' && pollingInterval) {
            poll();
        }
    }

    window.MandariNotifications = {
        init: init,
        poll: poll,
        getCount: () => lastCount,
        auswerten: auswerten,
        hinweis: hinweis,
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
