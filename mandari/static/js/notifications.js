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
        abrufFrist: 8000,     // danach gilt ein Abruf als gescheitert, vor dem nächsten
    };

    let pollingInterval = null;
    let lastCount = 0;
    let orgSlug = null;
    let konto = '';
    // Stand dieser Seite; er gilt, wenn der Browser nichts speichern lässt oder das Schreiben scheitert
    let merker;
    // Ein Abruf zur Zeit: Zwei gleichzeitige Antworten könnten sich überholen
    let laeuft = false;

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
        let gespeichert;
        try {
            const wert = window.localStorage.getItem(schluessel());
            if (wert !== null) gespeichert = wert;
        } catch (e) {
            // Speicher gesperrt: der Merker dieser Seite gilt
        }
        // Der spätere von beiden: Ein anderer Reiter kann weiter sein als diese Seite, und nach einem gescheiterten
        // Schreiben ist diese Seite weiter als der Speicher
        return spaeterer(gespeichert, merker);
    }

    /** Ob Zeitpunkt `a` nach `b` liegt; gleiche Millisekunde entscheidet die Schreibweise (Mikrosekunden) */
    function istSpaeter(a, b) {
        const za = Date.parse(a);
        const zb = Date.parse(b);
        if (Number.isNaN(za)) return false;
        if (Number.isNaN(zb)) return true;
        return za === zb ? a > b : za > zb;
    }

    /** Der spätere zweier Stände; `undefined` (nie gemerkt) und leer (noch keine Benachrichtigung) zählen als früher */
    function spaeterer(a, b) {
        if (a === undefined) return b;
        if (b === undefined) return a;
        if (a === '') return b;
        if (b === '') return a;
        return istSpaeter(b, a) ? b : a;
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
        if (laeuft) return;
        laeuft = true;
        // Bleibt eine Antwort aus, endet der Abruf nach der Frist; sonst stünden alle weiteren still
        const abbruch = new AbortController();
        const frist = setTimeout(() => abbruch.abort(), CONFIG.abrufFrist);
        const gemerkt = standLesen();
        const since = gemerkt ? `?since=${encodeURIComponent(gemerkt)}` : '';
        try {
            const response = await fetch(`/work/${orgSlug}/notifications/latest/${since}`, { signal: abbruch.signal });
            if (response.ok) {
                handleNotificationUpdate(await response.json());
            }
        } catch (error) {
            console.error('Notifications: Polling error', error);
        } finally {
            clearTimeout(frist);
            laeuft = false;
        }
    }

    /**
     * Was aus einer Antwort einzublenden ist und welcher Stand danach gilt. Die Antwort enthält ungelesene
     * Benachrichtigungen, die neueste zuerst. `gemerkt` ist der Stand beim Auswerten, nicht beim Absenden: Eingeblendet
     * wird nur, was danach liegt, und der Stand geht nie zurück. Eine verspätete Antwort (anderer Reiter) zeigt so
     * nichts doppelt. Beim ersten Besuch in einem Browser wird nichts nachgeholt: Die ungelesenen stehen in der Glocke.
     */
    function auswerten(notifications, gemerkt) {
        const liste = (Array.isArray(notifications) ? notifications : []).filter(
            (eintrag) => eintrag && typeof eintrag.created_at === 'string'
        );
        const neueste = liste.length ? liste[0].created_at : '';
        if (gemerkt === undefined) {
            return { zeigen: [], stand: neueste };
        }
        const zeigen = gemerkt === '' ? liste : liste.filter((eintrag) => istSpaeter(eintrag.created_at, gemerkt));
        return { zeigen: zeigen, stand: spaeterer(gemerkt, neueste) };
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

    function handleNotificationUpdate(data) {
        const count = Number(data.count) || 0;
        updateTitle(count);
        lastCount = count;

        const gemerkt = standLesen();
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
