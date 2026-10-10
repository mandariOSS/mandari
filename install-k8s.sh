#!/usr/bin/env bash
# =============================================================================
# mandari – Installer für Kubernetes
# =============================================================================
# Installiert mandari per Helm in einen bestehenden Cluster.
#
#   ./install-k8s.sh                          # geführt
#   ./install-k8s.sh --unattended             # ohne Rückfragen (Umgebungsvariablen)
#   ./install-k8s.sh --domain ris.example.de --namespace mandari
#   ./install-k8s.sh --dry-run                # nur anzeigen, nichts ändern
#   ./install-k8s.sh --minimal                # kleiner Cluster (ohne Elasticsearch)
#   ./install-k8s.sh --website-host www.example.de   # mit Marketing-Website unter eigenem Host
#   ./install-k8s.sh --ingress-class traefik  # Ingress-Klasse vorgeben (sonst erkannt)
#   ./install-k8s.sh --tag v0.11.0            # feste Version (https://github.com/mandariOSS/mandari/releases)
#   ./install-k8s.sh --uninstall              # entfernen (Daten bleiben)
#
# Voraussetzungen: kubectl mit Zugriff auf den Cluster. Helm wird bei Bedarf
# installiert. Für HTTPS werden ein Ingress-Controller und cert-manager benötigt.
# Den Ingress-Controller erkennt der Installer (k3s: Traefik), er installiert keinen.
# cert-manager bietet er bei Bedarf an.
#
# Für einen einzelnen Server ohne Kubernetes: ./install.sh (Docker Compose)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="$SCRIPT_DIR/deploy/kubernetes/helm/mandari"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

log()   { echo -e "${GREEN}[MANDARI]${NC} $1"; }
warn()  { echo -e "${YELLOW}[HINWEIS]${NC} $1"; }
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
error() { echo -e "${RED}[FEHLER]${NC} $1" >&2; exit 1; }

# Vorgaben (per Umgebungsvariable oder Argument überschreibbar)
NAMESPACE="${NAMESPACE:-mandari}"
RELEASE="${RELEASE:-mandari}"
DOMAIN="${DOMAIN:-}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
STORAGE_CLASS="${STORAGE_CLASS:-}"
ADMIN_EMAIL="${ADMIN_EMAIL:-}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
CLUSTER_ISSUER="${CLUSTER_ISSUER:-letsencrypt-prod}"
ACME_EMAIL="${ACME_EMAIL:-}"
# Leer = erkennen (Standardklasse des Clusters, auf k3s traefik)
INGRESS_CLASS="${INGRESS_CLASS:-}"
WEBSITE_HOST="${WEBSITE_HOST:-}"
UNATTENDED=false
DRY_RUN=false
MINIMAL=false
UNINSTALL=false
WITH_WEBSITE=false

show_banner() {
    echo -e "${CYAN}"
    cat <<'BANNER'
  __  __                 _            _
 |  \/  | __ _ _ __   __| | __ _ _ __(_)
 | |\/| |/ _` | '_ \ / _` |/ _` | '__| |
 | |  | | (_| | | | | (_| | (_| | |  | |
 |_|  |_|\__,_|_| |_|\__,_|\__,_|_|  |_|

 Installation in Kubernetes
 Open Source unter AGPL-3.0-or-later
BANNER
    echo -e "${NC}"
}

# -----------------------------------------------------------------------------
# Argumente
# -----------------------------------------------------------------------------
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --unattended)    UNATTENDED=true ;;
            --dry-run)       DRY_RUN=true ;;
            --minimal)       MINIMAL=true ;;
            --uninstall)     UNINSTALL=true ;;
            --with-website)  WITH_WEBSITE=true ;;
            --domain)        DOMAIN="${2:?--domain benötigt einen Wert}"; shift ;;
            --domain=*)      DOMAIN="${1#--domain=}" ;;
            --namespace|-n)  NAMESPACE="${2:?--namespace benötigt einen Wert}"; shift ;;
            --namespace=*)   NAMESPACE="${1#--namespace=}" ;;
            --release)       RELEASE="${2:?--release benötigt einen Wert}"; shift ;;
            --release=*)     RELEASE="${1#--release=}" ;;
            --tag)           IMAGE_TAG="${2:?--tag benötigt einen Wert}"; shift ;;
            --tag=*)         IMAGE_TAG="${1#--tag=}" ;;
            --storage-class) STORAGE_CLASS="${2:?--storage-class benötigt einen Wert}"; shift ;;
            --storage-class=*) STORAGE_CLASS="${1#--storage-class=}" ;;
            --ingress-class) INGRESS_CLASS="${2:?--ingress-class benötigt einen Wert}"; shift ;;
            --ingress-class=*) INGRESS_CLASS="${1#--ingress-class=}" ;;
            --website-host)  WEBSITE_HOST="${2:?--website-host benötigt einen Wert}"; WITH_WEBSITE=true; shift ;;
            --website-host=*) WEBSITE_HOST="${1#--website-host=}"; WITH_WEBSITE=true ;;
            -h|--help)
                sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
                exit 0 ;;
            *) error "Unbekanntes Argument: $1 (Hilfe: --help)" ;;
        esac
        shift
    done
}

# -----------------------------------------------------------------------------
# Voraussetzungen
# -----------------------------------------------------------------------------
check_kubectl() {
    command -v kubectl &>/dev/null || error "kubectl fehlt. Anleitung: https://kubernetes.io/docs/tasks/tools/"

    if ! kubectl cluster-info &>/dev/null; then
        error "Keine Verbindung zum Cluster.
  Aktueller Kontext: $(kubectl config current-context 2>/dev/null || echo 'keiner')
  Prüfen mit: kubectl cluster-info"
    fi

    local version context
    version=$(kubectl version -o json 2>/dev/null | grep -o '"gitVersion": *"v[0-9.]*"' | head -2 | tail -1 | grep -o 'v[0-9.]*' || echo "unbekannt")
    context=$(kubectl config current-context 2>/dev/null || echo "unbekannt")
    log "Cluster: $context (Kubernetes $version)"
}

install_helm() {
    if command -v helm &>/dev/null; then
        log "Helm: $(helm version --short 2>/dev/null || echo vorhanden)"
        return
    fi
    warn "Helm nicht gefunden."
    if [ "$UNATTENDED" != "true" ]; then
        read -r -p "  Helm jetzt installieren? [J/n]: " answer
        [[ "$answer" =~ ^[Nn]$ ]] && error "Helm wird benötigt: https://helm.sh/docs/intro/install/"
    fi
    log "Installiere Helm..."
    curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash \
        || error "Helm-Installation fehlgeschlagen: https://helm.sh/docs/intro/install/"
    command -v helm &>/dev/null || error "Helm nach der Installation nicht gefunden."
}

# k3s erkennen: Die Kubelet-Version trägt dort den Zusatz "+k3s".
is_k3s() {
    kubectl get nodes -o jsonpath='{.items[*].status.nodeInfo.kubeletVersion}' 2>/dev/null | grep -q '+k3s'
}

# Ingress-Klasse wählen. Der Installer installiert keinen Ingress-Controller: k3s bringt Traefik
# mit, ein zweiter Controller konkurrierte um die Ports 80/443 (ingress-nginx ist zudem eingestellt).
detect_ingress_class() {
    if [ -n "$INGRESS_CLASS" ]; then
        if kubectl get ingressclass "$INGRESS_CLASS" &>/dev/null; then
            log "Ingress-Klasse „$INGRESS_CLASS“ vorhanden"
            return 0
        fi
        warn "Ingress-Klasse „$INGRESS_CLASS“ gibt es im Cluster nicht."
        INGRESS_CLASS=""
    fi

    local available default_class
    available=$(kubectl get ingressclass -o name 2>/dev/null | sed 's|ingressclass.networking.k8s.io/||' | tr '\n' ' ' | sed 's/ *$//')
    default_class=$(kubectl get ingressclass -o jsonpath='{.items[?(@.metadata.annotations.ingressclass\.kubernetes\.io/is-default-class=="true")].metadata.name}' 2>/dev/null | awk '{print $1}')

    if [ -z "$available" ]; then
        if is_k3s; then
            warn "k3s erkannt, aber keine Ingress-Klasse gefunden (mit --disable traefik gestartet?)."
        else
            warn "Kein Ingress-Controller im Cluster gefunden."
        fi
        return 1
    fi

    if is_k3s && [[ " $available " == *" traefik "* ]]; then
        INGRESS_CLASS="traefik"
        log "k3s erkannt: verwende den mitgelieferten Ingress-Controller Traefik"
    elif [ -n "$default_class" ]; then
        INGRESS_CLASS="$default_class"
        log "Verwende die Standard-Ingress-Klasse „$INGRESS_CLASS“"
    elif [ "$(wc -w <<<"$available")" -eq 1 ]; then
        INGRESS_CLASS="$available"
        log "Verwende die einzige Ingress-Klasse „$INGRESS_CLASS“"
    elif [ "$UNATTENDED" = "true" ]; then
        warn "Mehrere Ingress-Klassen ($available), keine ist Standard. Bitte --ingress-class angeben."
        return 1
    else
        info "Ingress-Klassen: $available"
        while [ -z "$INGRESS_CLASS" ]; do
            read -r -p "  Welche verwenden? " INGRESS_CLASS
            if [ -n "$INGRESS_CLASS" ] && ! kubectl get ingressclass "$INGRESS_CLASS" &>/dev/null; then
                warn "„$INGRESS_CLASS“ gibt es nicht."
                INGRESS_CLASS=""
            fi
        done
    fi
    return 0
}

# Ingress-Controller und cert-manager sind Cluster-weite Bausteine. Ohne sie ist die
# Installation zwar erreichbar (Port-Forward), aber nicht über die Domain mit HTTPS.
check_cluster_addons() {
    local has_certmanager=false

    if ! detect_ingress_class; then
        NO_INGRESS=true
        warn "Ohne Ingress-Controller ist mandari nur clusterintern erreichbar (Port-Forward)."
        echo "  Einen Controller installieren Sie selbst, z. B. Traefik: https://doc.traefik.io/traefik/getting-started/install-traefik/"
        echo "  Danach erneut aufrufen, bei mehreren Klassen mit --ingress-class <name>."
    fi

    if kubectl get crd certificates.cert-manager.io &>/dev/null; then
        has_certmanager=true
        log "cert-manager vorhanden"
    else
        warn "cert-manager fehlt (wird für automatische HTTPS-Zertifikate gebraucht)."
    fi

    if [ "$has_certmanager" = true ]; then
        return
    fi
    if [ "${NO_INGRESS:-false}" = true ] || [ "$UNATTENDED" = "true" ]; then
        [ "$UNATTENDED" = "true" ] && warn "cert-manager wird im unbeaufsichtigten Modus nicht installiert, HTTPS bleibt aus."
        NO_TLS=true
        return
    fi
    echo ""
    echo "  cert-manager holt Let's-Encrypt-Zertifikate und kann jetzt installiert werden."
    read -r -p "  Jetzt installieren? [J/n]: " answer
    if [[ "$answer" =~ ^[Nn]$ ]]; then
        NO_TLS=true
        warn "Übersprungen. Die Anwendung ist dann nur über HTTP erreichbar."
    else
        install_cert_manager
    fi
}

install_cert_manager() {
    log "Installiere cert-manager..."
    helm repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true
    helm repo update jetstack >/dev/null 2>&1 || true
    helm upgrade --install cert-manager jetstack/cert-manager \
        --namespace cert-manager --create-namespace \
        --set crds.enabled=true \
        --wait --timeout 10m || error "Installation von cert-manager fehlgeschlagen."

    if [ -z "$ACME_EMAIL" ] && [ "$UNATTENDED" != "true" ]; then
        read -r -p "  E-Mail für Let's Encrypt (Ablaufwarnungen): " ACME_EMAIL
    fi
    if [ -n "$ACME_EMAIL" ]; then
        log "Lege ClusterIssuer „$CLUSTER_ISSUER“ an..."
        kubectl apply -f - <<EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: ${CLUSTER_ISSUER}
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ${ACME_EMAIL}
    privateKeySecretRef:
      name: ${CLUSTER_ISSUER}-account-key
    solvers:
      - http01:
          ingress:
            ingressClassName: ${INGRESS_CLASS}
EOF
    else
        warn "Ohne E-Mail kein ClusterIssuer – HTTPS muss manuell eingerichtet werden."
        NO_TLS=true
    fi
    log "cert-manager installiert"
}

# -----------------------------------------------------------------------------
# Abfragen
# -----------------------------------------------------------------------------
configure() {
    if [ "$UNATTENDED" = "true" ]; then
        [ -n "$DOMAIN" ] || error "--domain oder DOMAIN wird im unbeaufsichtigten Modus benötigt."
        return
    fi

    echo ""
    log "Konfiguration"
    echo "============================================"
    echo ""

    while [ -z "$DOMAIN" ]; do
        read -r -p "  Domain (z. B. ris.meine-kommune.de): " DOMAIN
        [ -z "$DOMAIN" ] && warn "Die Domain wird benötigt."
    done

    read -r -p "  Namespace [$NAMESPACE]: " input && NAMESPACE="${input:-$NAMESPACE}"

    local classes
    classes=$(kubectl get storageclass -o name 2>/dev/null | sed 's|storageclass.storage.k8s.io/||' | tr '\n' ' ')
    if [ -n "$classes" ]; then
        local default_class
        default_class=$(kubectl get storageclass -o jsonpath='{.items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")].metadata.name}' 2>/dev/null || true)
        info "Speicherklassen: $classes${default_class:+ (Standard: $default_class)}"
        read -r -p "  Speicherklasse [${default_class:-Standard}]: " input && STORAGE_CLASS="${input:-$STORAGE_CLASS}"
    else
        warn "Keine Speicherklasse gefunden – ohne persistenten Speicher gehen Daten beim Neustart verloren."
    fi

    echo ""
    log "Admin-Konto (leer lassen, um es später von Hand anzulegen)"
    read -r -p "  E-Mail: " ADMIN_EMAIL
    if [ -n "$ADMIN_EMAIL" ]; then
        while true; do
            read -r -s -p "  Passwort (mindestens 12 Zeichen): " ADMIN_PASSWORD; echo ""
            if [ "${#ADMIN_PASSWORD}" -lt 12 ]; then
                warn "Zu kurz."
                continue
            fi
            read -r -s -p "  Passwort wiederholen: " confirm; echo ""
            [ "$ADMIN_PASSWORD" = "$confirm" ] && break
            warn "Passwörter stimmen nicht überein."
        done
    fi

    echo ""
    read -r -p "  Marketing-Website mitinstallieren? [j/N]: " answer
    [[ "$answer" =~ ^[JjYy]$ ]] && WITH_WEBSITE=true

    if [ "$MINIMAL" = false ]; then
        echo ""
        info "Elasticsearch beschleunigt die Volltextsuche, braucht aber etwa 2 GB Arbeitsspeicher."
        read -r -p "  Elasticsearch installieren? [J/n]: " answer
        [[ "$answer" =~ ^[Nn]$ ]] && MINIMAL=true
    fi
    # Ausdrücklich 0: Ein falscher Vergleich als letzter Befehl beendete das Skript sonst still (set -e)
    return 0
}

# -----------------------------------------------------------------------------
# Installation
# -----------------------------------------------------------------------------
build_helm_args() {
    HELM_ARGS=(
        "$RELEASE" "$CHART_DIR"
        --namespace "$NAMESPACE"
        --create-namespace
        --set "domain=$DOMAIN"
        --set "image.tag=$IMAGE_TAG"
    )
    [ -n "$INGRESS_CLASS" ] && HELM_ARGS+=(--set "ingress.className=$INGRESS_CLASS")
    [ "$MINIMAL" = true ] && HELM_ARGS+=(--values "$CHART_DIR/values-minimal.yaml")
    [ "$WITH_WEBSITE" = true ] && HELM_ARGS+=(--set website.enabled=true)
    [ -n "$WEBSITE_HOST" ] && HELM_ARGS+=(--set "website.host=$WEBSITE_HOST")
    [ -n "$STORAGE_CLASS" ] && HELM_ARGS+=(
        --set "postgres.storage.className=$STORAGE_CLASS"
        --set "redis.storage.className=$STORAGE_CLASS"
        --set "elasticsearch.storage.className=$STORAGE_CLASS"
        --set "persistence.media.className=$STORAGE_CLASS"
        --set "persistence.files.className=$STORAGE_CLASS"
        --set "ingestor.data.className=$STORAGE_CLASS"
        --set "website.media.className=$STORAGE_CLASS"
    )
    [ -n "$ADMIN_EMAIL" ] && HELM_ARGS+=(
        --set "adminUser.email=$ADMIN_EMAIL"
        --set "adminUser.password=$ADMIN_PASSWORD"
    )
    if [ "${NO_TLS:-false}" = true ]; then
        HELM_ARGS+=(--set ingress.tls.enabled=false)
    else
        HELM_ARGS+=(--set "ingress.tls.clusterIssuer=$CLUSTER_ISSUER")
    fi
    [ "${NO_INGRESS:-false}" = true ] && HELM_ARGS+=(--set ingress.enabled=false)
    # Ausdrücklich 0: Ein falscher Vergleich als letzter Befehl beendete das Skript sonst still (set -e)
    return 0
}

do_dry_run() {
    log "Testlauf – es wird nichts verändert."
    echo ""
    helm template "${HELM_ARGS[@]}" | head -60
    echo ""
    info "Vollständige Ausgabe: helm template ${HELM_ARGS[*]}"
}

do_install() {
    log "Installiere mandari in Namespace „$NAMESPACE“..."
    echo ""
    # Die Pods warten per initContainer auf den Migrations-Job; --wait-for-jobs meldet dessen Fehler direkt.
    helm upgrade --install "${HELM_ARGS[@]}" --wait --wait-for-jobs --timeout 15m \
        || error "Installation fehlgeschlagen. Ursache suchen mit:
  kubectl -n $NAMESPACE get pods
  kubectl -n $NAMESPACE describe pod -l app.kubernetes.io/instance=$RELEASE
  kubectl -n $NAMESPACE logs -l app.kubernetes.io/component=migrate --all-containers --tail=50
  kubectl -n $NAMESPACE logs -l app.kubernetes.io/instance=$RELEASE -c wait-for-schema --tail=20"

    log "Warte auf die Anwendung..."
    kubectl -n "$NAMESPACE" rollout status "deploy/$RELEASE" --timeout=10m \
        || warn "Die Anwendung ist noch nicht bereit. Status: kubectl -n $NAMESPACE get pods"
}

do_uninstall() {
    warn "Entfernt Release „$RELEASE“ aus Namespace „$NAMESPACE“."
    echo "  Daten (PersistentVolumeClaims) und Zugangsdaten bleiben erhalten."
    if [ "$UNATTENDED" != "true" ]; then
        read -r -p "  Fortfahren? [j/N]: " answer
        [[ "$answer" =~ ^[JjYy]$ ]] || { log "Abgebrochen."; exit 0; }
    fi
    helm uninstall "$RELEASE" --namespace "$NAMESPACE"
    log "Entfernt. Daten liegen weiterhin hier:"
    kubectl -n "$NAMESPACE" get pvc,secret -l "app.kubernetes.io/instance=$RELEASE" 2>/dev/null || true
    echo ""
    echo "  Vollständig löschen (nicht umkehrbar):"
    echo "    kubectl delete namespace $NAMESPACE"
}

show_summary() {
    local url="https://$DOMAIN"
    [ "${NO_TLS:-false}" = true ] && url="http://$DOMAIN"

    echo ""
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  Installation abgeschlossen${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""
    echo -e "  Adresse:    ${CYAN}${url}${NC}"
    echo -e "  Namespace:  ${CYAN}${NAMESPACE}${NC}"
    echo -e "  Version:    ${CYAN}${IMAGE_TAG}${NC}"
    echo ""

    if [ "${NO_INGRESS:-false}" != true ]; then
        local address
        address=$(kubectl -n "$NAMESPACE" get ingress "$RELEASE" \
            -o jsonpath='{.status.loadBalancer.ingress[0].ip}{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
        if [ -n "$address" ]; then
            echo -e "  ${YELLOW}DNS-Eintrag setzen:${NC} $DOMAIN  ->  $address"
        else
            echo -e "  ${YELLOW}DNS:${NC} $DOMAIN muss auf den Ingress-Controller zeigen."
            echo "       Adresse: kubectl -n $NAMESPACE get ingress $RELEASE"
        fi
        echo ""
    else
        echo "  Ohne Ingress erreichbar über:"
        echo "    kubectl -n $NAMESPACE port-forward svc/$RELEASE 8080:80"
        echo "    http://localhost:8080"
        echo ""
    fi

    echo -e "  ${YELLOW}Zugangsdaten sichern (enthält den Verschlüsselungsschlüssel):${NC}"
    echo "    kubectl -n $NAMESPACE get secret ${RELEASE}-secrets -o yaml > mandari-secrets-backup.yaml"
    echo ""
    echo "  Nützliche Befehle:"
    echo "    Status:      kubectl -n $NAMESPACE get pods"
    echo "    Logs:        kubectl -n $NAMESPACE logs -f deploy/$RELEASE"
    echo "    Aktualisieren: ./install-k8s.sh --domain $DOMAIN --namespace $NAMESPACE --tag <version>"
    echo "    Entfernen:   ./install-k8s.sh --uninstall --namespace $NAMESPACE"
    echo ""
    if [ -z "$ADMIN_EMAIL" ]; then
        echo "  Admin-Konto anlegen:"
        echo "    kubectl -n $NAMESPACE exec -it deploy/$RELEASE -- python manage.py createsuperuser"
        echo ""
    fi
    echo "  Dokumentation: https://docs.mandari.de"
    echo ""
}

# -----------------------------------------------------------------------------
main() {
    parse_args "$@"
    show_banner

    [ -d "$CHART_DIR" ] || error "Helm-Chart nicht gefunden: $CHART_DIR
  Das Skript gehört ins Wurzelverzeichnis des Repositorys."

    check_kubectl
    install_helm

    if [ "$UNINSTALL" = true ]; then
        do_uninstall
        exit 0
    fi

    configure
    check_cluster_addons
    build_helm_args

    if [ "$DRY_RUN" = true ]; then
        do_dry_run
        exit 0
    fi

    do_install
    show_summary
}

main "$@"
