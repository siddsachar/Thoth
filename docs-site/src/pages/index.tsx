import React from 'react';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';

export default function Home(): JSX.Element {
  return (
    <Layout
      title="Local-first AI workbench"
      description="Row-Bot public documentation and downloads"
    >
      <header className="hero hero--primary">
        <div className="container">
          <h1 className="hero__title">Row-Bot</h1>
          <p className="hero__subtitle">
            A local-first AI workbench for parent-led agents, durable documents,
            authenticated remote access, models, tools, workflows, studios,
            extensions, channels, and voice.
          </p>
          <div className="rowBotHeroActions">
            <Link className="button button--primary button--lg" to="/docs/">
              Read the docs
            </Link>
            <Link
              className="button button--secondary button--lg"
              href="https://row-bot.ai/#install"
            >
              Download
            </Link>
          </div>
        </div>
      </header>
      <main className="container">
        <section className="rowBotPanelGrid">
          <article className="rowBotPanel">
            <h3>Start quickly</h3>
            <p>
              Install Row-Bot, choose a local, hosted, subscription, or custom
              model path, and send your first useful prompt.
            </p>
            <Link to="/docs/getting-started/">Getting started</Link>
          </article>
          <article className="rowBotPanel">
            <h3>Learn the UI</h3>
            <p>
              Tour chat, coordinated agents, the status bar, Workflows,
              Developer and Designer Studios, Knowledge, Settings, and approvals.
            </p>
            <Link to="/docs/app-shell/navigation">Interface tour</Link>
          </article>
          <article className="rowBotPanel">
            <h3>Configure your setup</h3>
            <p>
              Connect providers, local models, tools, channels, extensions, and
              voice, or deploy the authenticated Docker and VPS server path.
            </p>
            <Link to="/docs/configuration/models-and-providers">
              Configuration
            </Link>
          </article>
        </section>
      </main>
    </Layout>
  );
}
