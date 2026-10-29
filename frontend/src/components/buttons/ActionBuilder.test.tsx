import { useState } from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ActionBuilder } from './ActionBuilder';

vi.mock('../../api/integrations', () => ({
  useIntegrations: () => ({
    data: [
      { entity_id: 'wled.field1', domain: 'wled', display_name: 'Field LEDs' },
      { entity_id: 'atem.main', domain: 'atem', display_name: 'ATEM' },
    ],
  }),
  useIntegrationSchemas: () => ({
    data: {
      wled: {
        services: {
          flash: { description: 'Flash', fields: { color: { type: 'string' }, count: { type: 'integer', required: true } } },
          set_power: { fields: { on: { type: 'boolean', required: true } } },
        },
      },
      atem: { services: { switch_input: { fields: { input_index: { type: 'integer', required: true } } } } },
    },
  }),
}));
vi.mock('../../api/scripts', () => ({ useScripts: () => ({ data: [{ name: 'reset_lights' }] }) }));
vi.mock('../../api/overlays', () => ({ useOverlays: () => ({ data: [{ entity_id: 'overlay-main', display_name: 'Main' }] }) }));

function Harness({ initial = '' }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <ActionBuilder value={value} onChange={setValue} />
      <pre data-testid="yaml">{value}</pre>
    </>
  );
}

const yamlOut = () => screen.getByTestId('yaml').textContent;

describe('ActionBuilder', () => {
  beforeEach(() => vi.clearAllMocks());

  it('builds a service step from the catalog pickers', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: /add step/i }));
    await user.selectOptions(screen.getByLabelText('Integration'), 'wled.field1');
    await user.selectOptions(screen.getByLabelText('Service'), 'flash');
    await user.type(screen.getByLabelText('count'), '3');
    await user.type(screen.getByLabelText('color'), '#FF0000');
    expect(yamlOut()).toBe('- service: wled.flash\n  target: wled.field1\n  data:\n    count: 3\n    color: \'#FF0000\'\n');
  });

  it('shows validation problems until the step is complete', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: /add step/i }));
    expect(screen.getByTestId('step-problems')).toHaveTextContent('Step 1: Pick a service');
    await user.selectOptions(screen.getByLabelText('Integration'), 'atem.main');
    await user.selectOptions(screen.getByLabelText('Service'), 'switch_input');
    expect(screen.queryByTestId('step-problems')).not.toBeInTheDocument();
  });

  it('adds script, delay, spotlight and http steps and reorders/removes them', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const add = async (label: string) => {
      await user.selectOptions(screen.getByLabelText('Step type'), label);
      await user.click(screen.getByRole('button', { name: /add step/i }));
    };
    await add('Run a script');
    await user.selectOptions(screen.getByLabelText('Script'), 'reset_lights');
    await add('Spotlight a team on the overlay');
    await user.type(screen.getByLabelText('Spotlight team'), '1234A');
    await user.selectOptions(screen.getByLabelText('Overlay'), 'overlay-main');
    await add('Send an HTTP request');
    await user.type(screen.getByLabelText('URL'), 'https://hooks.example.com/go');
    expect(yamlOut()).toContain('script: reset_lights');
    expect(yamlOut()).toContain('team: 1234A');
    expect(yamlOut()).toContain('url: https://hooks.example.com/go');

    const steps = screen.getAllByTestId('step');
    await user.click(within(steps[2]).getByRole('button', { name: 'Move step up' }));
    expect(yamlOut()!.indexOf('http:')).toBeLessThan(yamlOut()!.indexOf('spotlight:'));
    await user.click(within(screen.getAllByTestId('step')[1]).getByRole('button', { name: 'Remove step' }));
    expect(yamlOut()).not.toContain('http:');
    expect(yamlOut()).toContain('script: reset_lights');
    expect(yamlOut()).toContain('spotlight:');
  });

  it('opens existing simple YAML visually and keeps complex YAML in YAML mode', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<Harness initial={'- delay: 00:00:05\n- script: reset_lights\n'} />);
    expect(screen.getByTestId('visual-editor')).toBeInTheDocument();
    expect(screen.getByLabelText('Delay')).toHaveValue('00:00:05');
    unmount();

    render(<Harness initial={'- condition: "{{ true }}"\n- service: obs.x\n'} />);
    expect(screen.queryByTestId('visual-editor')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox')).toHaveValue('- condition: "{{ true }}"\n- service: obs.x\n');
    await user.click(screen.getByRole('tab', { name: 'Visual' }));
    expect(screen.getByText(/cannot show/i)).toBeInTheDocument();
    expect(screen.queryByTestId('visual-editor')).not.toBeInTheDocument();
  });

  it('switching from YAML to Visual re-parses edits made in YAML', async () => {
    const user = userEvent.setup();
    render(<Harness initial={'- delay: 00:00:05\n'} />);
    await user.click(screen.getByRole('tab', { name: 'YAML' }));
    const box = screen.getByRole('textbox');
    await user.clear(box);
    await user.click(box);
    await user.paste('- delay: 00:00:09\n');
    await user.click(screen.getByRole('tab', { name: 'Visual' }));
    expect(screen.getByLabelText('Delay')).toHaveValue('00:00:09');
  });
});
