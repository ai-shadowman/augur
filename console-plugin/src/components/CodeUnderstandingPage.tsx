import { DocumentTitle } from '@openshift-console/dynamic-plugin-sdk';
import { Button, PageSection, Spinner } from '@patternfly/react-core';
import { ExternalLinkAltIcon } from '@patternfly/react-icons';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import './code-understanding.css';

type PluginConfig = {
  appUrl?: string;
};

const CONFIG_URL = '/api/plugins/code-understanding-console/config.json';
const DEFAULT_BACKEND_URL = '/api/proxy/plugin/code-understanding-console/backend/';

function normalizeAppUrl(rawUrl?: string): string {
  const trimmed = (rawUrl || '').trim();
  if (!trimmed || trimmed.includes('code-understanding-plugin-api')) {
    return DEFAULT_BACKEND_URL;
  }
  return trimmed.endsWith('/') ? trimmed : `${trimmed}/`;
}

function escapeHtmlAttr(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// OpenShift Console's /api/proxy/* injects "Content-Security-Policy: sandbox" on responses,
// which blocks scripts and forces an opaque null origin when navigated to directly via <iframe src>.
// Loading the HTML via fetch() into <iframe srcDoc> keeps the Console origin so same-origin
// static assets and /api/* calls carry the session cookie and execute cleanly.
function prepareSrcDoc(rawHtml: string, proxyPath: string): string {
  const baseHref = escapeHtmlAttr(new URL(proxyPath, window.location.origin).href);
  const baseTag = `<base href="${baseHref}" />`;
  let html = /<head(\s[^>]*)?>/i.test(rawHtml)
    ? rawHtml.replace(/<head(\s[^>]*)?>/i, (match) => `${match}\n  ${baseTag}`)
    : `${baseTag}\n${rawHtml}`;

  const nonce = document.querySelector('script[nonce]')?.getAttribute('nonce') || '';
  if (nonce) {
    const safeNonce = escapeHtmlAttr(nonce);
    html = html
      .replace(/<script(?![^>]*\bnonce=)/gi, `<script nonce="${safeNonce}"`)
      .replace(/<style(?![^>]*\bnonce=)/gi, `<style nonce="${safeNonce}"`);
  }
  return html;
}

const CodeUnderstandingPage = () => {
  const { t } = useTranslation('plugin__code-understanding-console');
  const [appUrl, setAppUrl] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState<number>(() => Date.now());
  const [srcDoc, setSrcDoc] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(`${CONFIG_URL}?_t=${Date.now()}`, { cache: 'no-store' })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        return response.json() as Promise<PluginConfig>;
      })
      .then((config) => {
        if (!cancelled) {
          setAppUrl(normalizeAppUrl(config.appUrl));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setAppUrl(DEFAULT_BACKEND_URL);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin === window.location.origin && event.data?.type === 'cu-reload') {
        setReloadKey(Date.now());
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, []);

  const isProxyBackend = Boolean(appUrl && appUrl.startsWith('/api/proxy/'));

  useEffect(() => {
    if (!appUrl || !isProxyBackend) {
      setSrcDoc(null);
      setLoadError(null);
      return;
    }
    let cancelled = false;
    setLoadError(null);
    const sep = appUrl.includes('?') ? '&' : '?';
    fetch(`${appUrl}${sep}_t=${reloadKey}`, {
      credentials: 'same-origin',
      cache: 'no-store',
    })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`HTTP ${response.status} ${response.statusText}`.trim());
        }
        return response.text();
      })
      .then((html) => {
        if (!cancelled) {
          setSrcDoc(prepareSrcDoc(html, appUrl));
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setLoadError(err instanceof Error ? err.message : String(err));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [appUrl, isProxyBackend, reloadKey]);

  const externalIframeSrc =
    appUrl && !isProxyBackend
      ? `${appUrl}${appUrl.includes('?') ? '&' : '?'}_t=${reloadKey}`
      : null;

  const newTabHref = isProxyBackend ? '/code-understanding' : appUrl || '/code-understanding';
  const isLoading = !appUrl || (isProxyBackend && !srcDoc && !loadError);

  return (
    <>
      <DocumentTitle>{t('Code Understanding')}</DocumentTitle>
      <PageSection>
        {isLoading && <Spinner size="lg" aria-label={t('Loading console')} />}
        {loadError && (
          <div className="cu-console-error" role="alert">
            <p>{t('Could not load the Code Understanding console URL.')} ({loadError})</p>
            <Button variant="secondary" onClick={() => setReloadKey(Date.now())}>
              {t('Retry')}
            </Button>
          </div>
        )}
        {!isLoading && !loadError && (
          <>
            <div className="cu-console-toolbar">
              <Button
                component="a"
                href={newTabHref}
                target="_blank"
                rel="noopener noreferrer"
                variant="link"
                icon={<ExternalLinkAltIcon />}
              >
                {t('Open in new tab')}
              </Button>
            </div>
            {isProxyBackend && srcDoc ? (
              <iframe
                key={reloadKey}
                className="cu-console-frame"
                title={t('Code Understanding')}
                srcDoc={srcDoc}
                allow="clipboard-read; clipboard-write"
              />
            ) : (
              externalIframeSrc && (
                <iframe
                  key={reloadKey}
                  className="cu-console-frame"
                  title={t('Code Understanding')}
                  src={externalIframeSrc}
                  allow="clipboard-read; clipboard-write"
                />
              )
            )}
          </>
        )}
      </PageSection>
    </>
  );
};

export default CodeUnderstandingPage;
