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

const CodeUnderstandingPage = () => {
  const { t } = useTranslation('plugin__code-understanding-console');
  const [appUrl, setAppUrl] = useState<string | null>(null);
  const [mountTs] = useState<number>(() => Date.now());

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

  const iframeSrc = appUrl
    ? `${appUrl}${appUrl.includes('?') ? '&' : '?'}_t=${mountTs}`
    : null;

  return (
    <>
      <DocumentTitle>{t('Code Understanding')}</DocumentTitle>
      <PageSection>
        {!appUrl && (
          <Spinner size="lg" aria-label={t('Loading console')} />
        )}
        {appUrl && iframeSrc && (
          <>
            <div className="cu-console-toolbar">
              <Button
                component="a"
                href={appUrl}
                target="_blank"
                rel="noopener noreferrer"
                variant="link"
                icon={<ExternalLinkAltIcon />}
              >
                {t('Open in new tab')}
              </Button>
            </div>
            <iframe
              className="cu-console-frame"
              title={t('Code Understanding')}
              src={iframeSrc}
              allow="clipboard-read; clipboard-write"
            />
          </>
        )}
      </PageSection>
    </>
  );
};

export default CodeUnderstandingPage;
