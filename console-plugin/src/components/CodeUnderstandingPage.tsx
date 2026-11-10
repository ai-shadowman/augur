import { DocumentTitle } from '@openshift-console/dynamic-plugin-sdk';
import { Alert, Button, PageSection, Spinner } from '@patternfly/react-core';
import { ExternalLinkAltIcon } from '@patternfly/react-icons';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import './code-understanding.css';

type PluginConfig = {
  appUrl: string;
};

const CONFIG_URL = '/api/plugins/code-understanding-console/config.json';

const CodeUnderstandingPage = () => {
  const { t } = useTranslation('plugin__code-understanding-console');
  const [appUrl, setAppUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(CONFIG_URL)
      .then((response) => {
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        return response.json() as Promise<PluginConfig>;
      })
      .then((config) => {
        if (!cancelled) {
          setAppUrl(config.appUrl);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setError(t('Could not load the Code Understanding console URL.'));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [t]);

  return (
    <>
      <DocumentTitle>{t('Code Understanding')}</DocumentTitle>
      <PageSection>
        {error && <Alert variant="danger" title={error} isInline />}
        {!appUrl && !error && (
          <Spinner size="lg" aria-label={t('Loading console')} />
        )}
        {appUrl && (
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
              src={appUrl}
              allow="clipboard-read; clipboard-write"
            />
          </>
        )}
      </PageSection>
    </>
  );
};

export default CodeUnderstandingPage;
