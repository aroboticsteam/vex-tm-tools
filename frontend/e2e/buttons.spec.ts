import { expect, test } from '@playwright/test';
import { listPages, ok, uid } from './helpers';

test.describe('button pages', () => {
  const created: string[] = [];

  test.afterEach(async ({ request }) => {
    for (const page of await listPages(request)) {
      if (page.name.startsWith('e2e-')) await request.delete(`/api/v1/button-pages/${page.id}`);
    }
    created.length = 0;
  });

  test('build a button with the visual builder, press it and see the result', async ({ page, request }) => {
    const name = uid('e2e-page');
    await page.goto('/buttons');
    await page.getByRole('button', { name: 'New page' }).click();
    await page.getByLabel('Page name').fill(name);
    await page.getByRole('dialog').getByRole('button', { name: 'Save' }).click();
    await expect(page.getByRole('tab', { name })).toBeVisible();

    await page.getByRole('button', { name: /^Edit$/ }).click();
    await page.getByRole('button', { name: 'Add button' }).click();
    const dialog = page.getByRole('dialog', { name: 'New button' });
    await dialog.getByLabel('Label', { exact: true }).fill('Pause 1s');
    await dialog.getByLabel('Icon').fill('⏱');

    // Replace the default step with a short delay using the visual builder.
    await dialog.getByRole('button', { name: 'Remove step' }).click();
    await dialog.getByLabel('Step type').selectOption({ label: 'Wait (delay)' });
    await dialog.getByRole('button', { name: 'Add step' }).click();
    await dialog.getByLabel('Delay').fill('00:00:01');
    await dialog.getByRole('button', { name: 'Save' }).click();
    await expect(page.getByTestId('pad-button').filter({ hasText: 'Pause 1s' })).toBeVisible();

    // What the builder saved is the YAML the engine understands.
    const [stored] = (await listPages(request)).filter((p) => p.name === name);
    const detail = await ok<{ buttons: { action_yaml: string; icon: string }[] }[]>(await request.get('/api/v1/button-pages'));
    const button = detail.flatMap((p) => p.buttons).find((b) => b.action_yaml.includes('00:00:01'));
    expect(stored.buttons).toHaveLength(1);
    expect(button?.icon).toBe('⏱');

    await page.getByRole('button', { name: 'Done editing' }).click();
    const pad = page.getByTestId('pad-button').filter({ hasText: 'Pause 1s' });
    await pad.click();
    await expect(pad).toHaveAttribute('data-state', 'success');
    await expect(pad).toHaveAttribute('data-state', 'idle', { timeout: 5000 }); // feedback clears itself
  });

  test('a risky button asks before running and does nothing when declined', async ({ page, request }) => {
    const name = uid('e2e-confirm');
    const created = await ok<{ id: string }>(await request.post('/api/v1/button-pages', { data: { name, columns: 3 } }));
    await ok(
      await request.post(`/api/v1/button-pages/${created.id}/buttons`, {
        data: { label: 'Danger', action_yaml: '- delay: "00:00:01"\n', confirm: true },
      }),
    );
    await page.goto('/buttons');
    await page.getByRole('tab', { name }).click();

    const asked: string[] = [];
    page.once('dialog', async (dialog) => {
      asked.push(dialog.message());
      await dialog.dismiss();
    });
    const pad = page.getByTestId('pad-button').filter({ hasText: 'Danger' });
    await pad.click();
    await expect.poll(() => asked.length).toBe(1);
    expect(asked[0]).toContain('Danger');
    await expect(pad).toHaveAttribute('data-state', 'idle');

    page.once('dialog', (dialog) => void dialog.accept());
    await pad.click();
    await expect(pad).toHaveAttribute('data-state', 'success');
  });

  test('a failing action shows the failed state and the error', async ({ page, request }) => {
    const name = uid('e2e-fail');
    const created = await ok<{ id: string }>(await request.post('/api/v1/button-pages', { data: { name } }));
    await ok(
      await request.post(`/api/v1/button-pages/${created.id}/buttons`, {
        data: { label: 'Broken', action_yaml: '- service: nothing.doesnt_exist\n  target: nothing.missing\n' },
      }),
    );
    await page.goto('/buttons');
    await page.getByRole('tab', { name }).click();
    const pad = page.getByTestId('pad-button').filter({ hasText: 'Broken' });
    await pad.click();
    await expect(pad).toHaveAttribute('data-state', 'failed');
    await expect(page.getByText(/Broken failed/)).toBeVisible();
  });

  test('drag and drop reorders buttons and the order survives a reload', async ({ page, request }) => {
    const name = uid('e2e-order');
    const created = await ok<{ id: string }>(await request.post('/api/v1/button-pages', { data: { name, columns: 3 } }));
    for (const [position, label] of ['One', 'Two', 'Three'].entries()) {
      await ok(await request.post(`/api/v1/button-pages/${created.id}/buttons`, { data: { label, position, action_yaml: '- delay: 1\n' } }));
    }
    await page.goto('/buttons');
    await page.getByRole('tab', { name }).click();
    await page.getByRole('button', { name: /^Edit$/ }).click();

    const pads = page.getByTestId('pad-button');
    await expect(pads).toHaveText([/One/, /Two/, /Three/]);
    await pads.filter({ hasText: 'Three' }).dragTo(pads.filter({ hasText: 'One' }));
    await expect(pads).toHaveText([/Three/, /One/, /Two/]);

    await page.reload();
    await page.getByRole('tab', { name }).click();
    await expect(page.getByTestId('pad-button')).toHaveText([/Three/, /One/, /Two/]);
  });

  test('pages can be reordered by dragging their tabs', async ({ page, request }) => {
    const a = uid('e2e-a');
    const b = uid('e2e-b');
    await ok(await request.post('/api/v1/button-pages', { data: { name: a, position: 900 } }));
    await ok(await request.post('/api/v1/button-pages', { data: { name: b, position: 901 } }));
    await page.goto('/buttons');
    await page.getByRole('button', { name: /^Edit$/ }).click();
    await page.getByRole('tab', { name: b }).dragTo(page.getByRole('tab', { name: a }));
    await expect
      .poll(async () => (await listPages(request)).filter((p) => p.name === a || p.name === b).map((p) => p.name))
      .toEqual([b, a]);
  });
});
