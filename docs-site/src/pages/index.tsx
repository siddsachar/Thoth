import React from 'react';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';

export default function Home(): JSX.Element {
  return (
    <Layout
      title="Local-first desktop AI assistant"
      description="Row-Bot public documentation and downloads"
    >
      <header className="hero hero--primary">
        <div className="container">
          <h1 className="hero__title">Row-Bot</h1>
          <p className="hero__subtitle">
            A local-first desktop AI assistant for models, memory, tools,
            workflows, Designer Studio, Developer Studio, Skills Hub, plugins,
            MCP, channels, and voice.
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
              Tour chat, the status bar, Workflows, Designer Studio, Developer
              Studio, Knowledge, Settings, and approvals.
            </p>
            <Link to="/docs/ui-tour/">UI tour</Link>
          </article>
          <article className="rowBotPanel">
            <h3>Configure your setup</h3>
            <p>
              Connect providers, local models, embeddings, tools, channels,
              skills, plugins, MCP servers, Buddy, and voice.
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
