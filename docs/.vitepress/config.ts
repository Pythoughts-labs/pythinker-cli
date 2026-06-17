import { defineConfig } from 'vitepress'
import { withMermaid } from 'vitepress-plugin-mermaid'
import llmstxt from 'vitepress-plugin-llms'

const rawBase = process.env.VITEPRESS_BASE
const base = rawBase
  ? rawBase.startsWith('/')
    ? rawBase.endsWith('/') ? rawBase : `${rawBase}/`
    : `/${rawBase}/`
  : '/'

export default withMermaid(defineConfig({
  base,
  title: 'Pythinker Code Docs',
  description: 'Pythinker Code Documentation',

  locales: {
    en: {
      label: 'English',
      lang: 'en-US',
      link: '/en/',
      title: 'Pythinker Code Docs',
      description: 'Pythinker Code User Documentation',
      themeConfig: {
        nav: [
          { text: 'Guides', link: '/en/guides/getting-started', activeMatch: '/en/guides/' },
          { text: 'Customization', link: '/en/customization/mcp', activeMatch: '/en/customization/' },
          { text: 'Configuration', link: '/en/configuration/config-files', activeMatch: '/en/configuration/' },
          { text: 'Reference', link: '/en/reference/pythinker-command', activeMatch: '/en/reference/' },
          { text: 'FAQ', link: '/en/faq' },
          { text: 'Release Notes', link: '/en/release-notes/changelog', activeMatch: '/en/release-notes/' },
        ],
        sidebar: {
          '/en/guides/': [
            {
              text: 'Guides',
              items: [
                { text: 'Getting Started', link: '/en/guides/getting-started' },
                { text: 'Common Use Cases', link: '/en/guides/use-cases' },
                { text: 'Interaction and Input', link: '/en/guides/interaction' },
                { text: 'Sessions and Context', link: '/en/guides/sessions' },
                { text: 'Using in IDEs', link: '/en/guides/ides' },
              ],
            },
          ],
          '/en/customization/': [
            {
              text: 'Customization',
              items: [
                { text: 'Model Context Protocol', link: '/en/customization/mcp' },
                { text: 'Language Server Protocol', link: '/en/customization/lsp' },
                { text: 'Plugins (Beta)', link: '/en/customization/plugins' },
                { text: 'Hooks (Beta)', link: '/en/customization/hooks' },
                { text: 'Agent Skills', link: '/en/customization/skills' },
                { text: 'Agents and Subagents', link: '/en/customization/agents' },
                { text: 'Agent Architecture', link: '/en/customization/agent-architecture' },
                { text: 'Repository Map', link: '/en/customization/architecture' },
                { text: 'Print Mode', link: '/en/customization/print-mode' },
                { text: 'Wire Mode', link: '/en/customization/wire-mode' },
              ],
            },
          ],
          '/en/configuration/': [
            {
              text: 'Configuration',
              items: [
                { text: 'Config Files', link: '/en/configuration/config-files' },
                { text: 'Providers and Models', link: '/en/configuration/providers' },
                { text: 'Config Overrides', link: '/en/configuration/overrides' },
                { text: 'Environment Variables', link: '/en/configuration/env-vars' },
                { text: 'Data Locations', link: '/en/configuration/data-locations' },
              ],
            },
          ],
          '/en/reference/': [
            {
              text: 'Reference',
              items: [
                { text: 'pythinker Command', link: '/en/reference/pythinker-command' },
                { text: 'pythinker info Subcommand', link: '/en/reference/pythinker-info' },
                { text: 'pythinker acp Subcommand', link: '/en/reference/pythinker-acp' },
                { text: 'pythinker mcp Subcommand', link: '/en/reference/pythinker-mcp' },
                { text: 'pythinker term Subcommand', link: '/en/reference/pythinker-term' },
                { text: 'pythinker dashboard Subcommand', link: '/en/reference/pythinker-dashboard' },
                { text: 'pythinker web Subcommand', link: '/en/reference/pythinker-web' },
                { text: 'Slash Commands', link: '/en/reference/slash-commands' },
                { text: 'Keyboard Shortcuts', link: '/en/reference/keyboard' },
              ],
            },
          ],
          '/en/release-notes/': [
            {
              text: 'Release Notes',
              items: [
                { text: 'Changelog', link: '/en/release-notes/changelog' },
                { text: 'Breaking Changes and Migration', link: '/en/release-notes/breaking-changes' },
              ],
            },
          ],
        },
      },
    },
  },

  themeConfig: {
    outline: [2, 3],
    search: { provider: 'local' },
    socialLinks: [
      { icon: 'github', link: 'https://github.com/Pythoughts-labs/pythinker-code' },
    ],
  },

  transformHead({ pageData }) {
    if (pageData.relativePath === 'index.md') {
      return [['meta', { 'http-equiv': 'refresh', content: `0; url=${base}en/` }]]
    }
  },

  vite: {
    plugins: [llmstxt()],
  },
}))
