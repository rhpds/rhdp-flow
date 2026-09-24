import { render, screen } from '@testing-library/react';
import { QATab } from '../QATab';
import { mockQAResult } from '../../test/mocks/api';

const noop = () => {};

describe('QATab', () => {
  it('renders empty state when no QA results', () => {
    render(<QATab qaResults={[]} setQAResults={noop} showToast={noop} />);
    expect(screen.getByText('No QA results yet')).toBeInTheDocument();
  });

  it('renders QA type selector', () => {
    render(<QATab qaResults={[]} setQAResults={noop} showToast={noop} />);
    expect(screen.getByLabelText('QA type')).toBeInTheDocument();
  });

  it('renders guidance alert', () => {
    render(<QATab qaResults={[]} setQAResults={noop} showToast={noop} />);
    expect(screen.getByText('When to use QA')).toBeInTheDocument();
  });

  it('renders results table with QA data', () => {
    render(<QATab qaResults={[mockQAResult]} setQAResults={noop} showToast={noop} />);
    expect(screen.getByText('QA Results (1)')).toBeInTheDocument();
    expect(screen.getByText('Test Workshop')).toBeInTheDocument();
    expect(screen.getByText('test-ns')).toBeInTheDocument();
  });

  it('shows Run QA button', () => {
    render(<QATab qaResults={[]} setQAResults={noop} showToast={noop} />);
    expect(screen.getByText('Run QA')).toBeInTheDocument();
  });

  it('defaults namespace scope to the only schedule namespace', () => {
    const schedules = [
      {
        ci_name: 'W',
        ci: 'w.prod',
        namespace: 'user-bbethell-redhat-com',
        enable_workshop_interface: true,
        password: '',
        activity: 'Workshops',
        purpose: 'QA',
        workshop_name: 'W',
        provisioning_date: '',
        auto_stop: '',
        auto_destroy: '',
      },
    ];
    render(
      <QATab
        qaResults={[]}
        setQAResults={noop}
        showToast={noop}
        schedules={schedules as never}
      />,
    );
    expect(screen.getByLabelText('QA namespace scope')).toHaveValue('user-bbethell-redhat-com');
    expect(screen.getByText(/Run QA for bbethell/)).toBeInTheDocument();
  });
});
