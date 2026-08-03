import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const landingPageUrl = 'https://row-bot.ai/';
const installationUrl = 'https://row-bot.ai/#install';
const marketingSitemapUrls = [
  'https://row-bot.ai/features.html',
  'https://row-bot.ai/architecture.html',
  'https://row-bot.ai/contact.html',
];

const config: Config = {
  title: 'Row-Bot',
  tagline: 'Reason. Orchestrate. Work.',
  favicon: 'favicon.ico',

  url: 'https://row-bot.ai',
  baseUrl: '/',

  organizationName: 'siddsachar',
  projectName: 'row-bot',

  onBrokenLinks: 'throw',
  trailingSlash: true,
  markdown: {
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          path: 'docs',
          routeBasePath: 'docs',
          sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/siddsachar/row-bot/edit/main/docs-site/',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
        sitemap: {
          async createSitemapItems({
            routes,
            siteConfig,
            defaultCreateSitemapItems,
          }) {
            const docsItems = await defaultCreateSitemapItems({routes, siteConfig});
            const existingUrls = new Set(docsItems.map((item) => item.url));
            const marketingItems = marketingSitemapUrls
              .filter((url) => !existingUrls.has(url))
              .map((url) => ({url}));
            return [...docsItems, ...marketingItems];
          },
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'img/brand/row_bot_preview.png',
    metadata: [
      {
        name: 'description',
        content:
          'Public documentation for Row-Bot, a local-first desktop AI assistant with models, memory, tools, workflows, Designer Studio, Developer Studio, Skills Hub, plugins, MCP, channels, and voice.',
      },
    ],
    navbar: {
      title: 'Row-Bot',
      logo: {
        alt: 'Row-Bot',
        src: 'img/brand/row_bot_glyph_256.png',
        href: landingPageUrl,
        target: '_self',
      },
      items: [
        {href: landingPageUrl, label: 'Home', position: 'left', target: '_self'},
        {to: '/docs/', label: 'Docs', position: 'left'},
        {to: '/docs/getting-started/installation', label: 'Install', position: 'left'},
        {to: '/docs/designer/', label: 'Designer', position: 'left'},
        {to: '/docs/developer/', label: 'Developer', position: 'left'},
        {to: '/docs/settings/', label: 'Settings', position: 'left'},
        {to: '/search', label: 'Search', position: 'left'},
        {
          href: 'https://github.com/siddsachar/row-bot',
          label: 'GitHub',
          position: 'right',
        },
        {
          href: installationUrl,
          label: 'Download',
          position: 'right',
          target: '_self',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Row-Bot',
          items: [
            {label: 'Home', href: landingPageUrl},
            {label: 'Features', href: 'https://row-bot.ai/features.html'},
            {label: 'Architecture', href: 'https://row-bot.ai/architecture.html'},
            {label: 'Contact', href: 'https://row-bot.ai/contact.html'},
            {label: 'Blog', href: 'https://sydsachar.com/category/row-bot/'},
          ],
        },
        {
          title: 'Docs',
          items: [
            {label: 'Getting Started', to: '/docs/getting-started/'},
            {label: 'Row-Bot Interface', to: '/docs/app-shell/navigation'},
            {label: 'Settings', to: '/docs/settings/'},
            {label: 'Troubleshooting', to: '/docs/troubleshooting/'},
          ],
        },
        {
          title: 'Project',
          items: [
            {label: 'GitHub', href: 'https://github.com/siddsachar/row-bot'},
            {label: 'Releases', href: 'https://github.com/siddsachar/row-bot/releases'},
            {label: 'Security', href: 'https://github.com/siddsachar/row-bot/blob/main/SECURITY.md'},
          ],
        },
        {
          title: 'Machine-readable',
          items: [
            {label: 'llms.txt', href: 'https://row-bot.ai/llms.txt'},
            {label: 'llms-full.txt', href: 'https://row-bot.ai/llms-full.txt'},
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Row-Bot contributors. Built with Docusaurus.`,
    },
    prism: {
      theme: require('prism-react-renderer').themes.github,
      darkTheme: require('prism-react-renderer').themes.dracula,
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
