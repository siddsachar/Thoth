import { useId, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { ChevronDown, ChevronRight, MessageSquare } from 'lucide-react';
import { useClientState, useRuntime } from '../../runtime';
import { useOverlay } from '../../ui/overlays';
import { Button, Hint, Skeleton } from '../../ui/primitives';

const PREVIEW_COUNT = 10;

/** Live store subscription also updates the compact modal's mounted content. */
export default function Navigation({
  onOpenConversation,
}: {
  onOpenConversation?: () => void;
}) {
  const state = useClientState();
  const { controller } = useRuntime();
  const overlay = useOverlay();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [sectionOpen, setSectionOpen] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const sectionId = useId();
  const selected =
    state.conversations.find(({ id }) => id === state.selectedConversationId) ??
    (state.conversation?.id === state.selectedConversationId
      ? state.conversation
      : null);
  const visible = expanded
    ? state.conversations
    : state.conversations.slice(0, PREVIEW_COUNT);
  const rows =
    selected && !visible.some(({ id }) => id === selected.id)
      ? [...visible, selected]
      : visible;
  function conversationRow(conversation: (typeof state.conversations)[number]) {
    const title = conversation.title || 'Untitled conversation';
    return (
      <li key={conversation.id}>
        <Hint label={title}>
          <Button
            variant="ghost"
            aria-label={title}
            aria-current={
              state.selectedConversationId === conversation.id
                ? 'page'
                : undefined
            }
            onClick={() => {
              void controller.selectConversation(conversation.id);
              if (pathname !== '/') navigate('/');
              onOpenConversation?.();
              overlay.close();
            }}
          >
            <MessageSquare size={15} aria-hidden />
            <span className="conversation-title">{title}</span>
          </Button>
        </Hint>
      </li>
    );
  }
  return (
    <nav className="navigation" aria-label="Workspace navigation">
      <Button
        className="nav-heading"
        variant="ghost"
        aria-expanded={sectionOpen}
        aria-controls={sectionId}
        onClick={() => setSectionOpen((open) => !open)}
      >
        {sectionOpen ? (
          <ChevronDown size={14} aria-hidden />
        ) : (
          <ChevronRight size={14} aria-hidden />
        )}
        Conversations
      </Button>
      {!sectionOpen && selected && (
        <ul className="conversation-list" aria-label="Current conversation">
          {conversationRow(selected)}
        </ul>
      )}
      <div id={sectionId} className="nav-conversations" hidden={!sectionOpen}>
        {sectionOpen && (
          <>
            {state.loadingConversations && rows.length === 0 ? (
              <Skeleton label="Loading conversations" />
            ) : rows.length === 0 ? (
              <p className="muted nav-empty">
                Your conversations will appear here.
              </p>
            ) : (
              <ul className="conversation-list" aria-label="Conversations">
                {rows.map(conversationRow)}
              </ul>
            )}
            {(state.conversations.length > PREVIEW_COUNT ||
              state.hasMoreConversations) && (
              <Button
                className="nav-more"
                variant="ghost"
                aria-expanded={expanded}
                onClick={() => setExpanded((show) => !show)}
              >
                {expanded ? 'Show less' : 'Show more'}
              </Button>
            )}
            {expanded && state.hasMoreConversations && (
              <Button
                className="nav-more"
                variant="ghost"
                onClick={() => {
                  void controller.loadMoreConversations();
                }}
                disabled={state.loadingConversations}
              >
                Load more conversations
              </Button>
            )}
          </>
        )}
      </div>
      <div className="nav-footer">
        <a href="/" className="button ghost">
          Current application
        </a>
        <Link className="button ghost" to="/primitives" onClick={overlay.close}>
          Component gallery
        </Link>
      </div>
    </nav>
  );
}
