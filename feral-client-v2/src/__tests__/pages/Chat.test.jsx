import { describe, it, expect } from 'vitest';
import { renderV2 } from '../_helpers/renderV2';
import Chat from '../../pages/Chat';

describe('Chat', () => {
  it('renders the seed assistant message + a text input', () => {
    const { getByText, container } = renderV2(<Chat />);
    expect(getByText('How can I help?')).toBeInTheDocument();
    expect(container.querySelector('.v2-chat-input')).toBeTruthy();
  });
});
