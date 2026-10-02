/** Shared status → CSS class mapping for deployment and QA results. */

import type { ComponentType, CSSProperties } from 'react';
import CheckCircleIcon from '@patternfly/react-icons/dist/esm/icons/check-circle-icon';
import ExclamationCircleIcon from '@patternfly/react-icons/dist/esm/icons/exclamation-circle-icon';
import ExclamationTriangleIcon from '@patternfly/react-icons/dist/esm/icons/exclamation-triangle-icon';
import InProgressIcon from '@patternfly/react-icons/dist/esm/icons/in-progress-icon';
import MinusCircleIcon from '@patternfly/react-icons/dist/esm/icons/minus-circle-icon';
import QuestionCircleIcon from '@patternfly/react-icons/dist/esm/icons/question-circle-icon';

export function statusColorClass(status: string): string {
  if (!status) return '';
  const s = status.toLowerCase();
  if (s.includes('verified') && !s.includes('unverified')) return 'status-verified';
  if (s.includes('unverified') || s.includes('no_url')) return 'status-deployed_unverified';
  if (s.includes('failed') || s.includes('error')) return 'status-failed';
  if (s === 'deleted' || s.includes('not yet deployed')) return 'status-deleted';
  return '';
}

/** Returns a PatternFly icon component matching the status for accessibility (color + icon). */
export function statusIcon(status: string): ComponentType<{ style?: CSSProperties }> | null {
  if (!status) return null;
  const s = status.toLowerCase();
  if ((s.includes('verified') && !s.includes('unverified')) || s === 'success') return CheckCircleIcon;
  if (s.includes('unverified') || s.includes('no_url')) return ExclamationTriangleIcon;
  if (s.includes('failed') || s.includes('error')) return ExclamationCircleIcon;
  if (s === 'deleted' || s.includes('not yet deployed')) return MinusCircleIcon;
  return null;
}

export interface StatusIndicator {
  icon: ComponentType<any>;
  color: string;
  label: string;
  className: string;
}

export const STATUS_LABEL_TO_PF_COLOR: Record<string, 'green' | 'red' | 'orange' | 'blue' | 'grey'> = {
  'Verified': 'green',
  'Failed': 'red',
  'Deployed (Unverified)': 'orange',
  'In Progress': 'blue',
  'Unknown': 'grey'
};

export function getStatusIndicator(status: string): StatusIndicator {
  const s = (status || '').toLowerCase();

  if (s.includes('verified') && !s.includes('unverified')) {
    return {
      icon: CheckCircleIcon,
      color: 'var(--pf-v6-global--success-color--100)',
      label: 'Verified',
      className: 'pf-v6-u-success-color-100',
    };
  }

  if (s.includes('deployed') && (s.includes('unverified') || s.includes('no_url'))) {
    return {
      icon: ExclamationTriangleIcon,
      color: 'var(--pf-v6-global--warning-color--100)',
      label: 'Deployed (Unverified)',
      className: 'pf-v6-u-warning-color-100',
    };
  }

  if (s.includes('failed') || s.includes('error')) {
    return {
      icon: ExclamationCircleIcon,
      color: 'var(--pf-v6-global--danger-color--100)',
      label: 'Failed',
      className: 'pf-v6-u-danger-color-100',
    };
  }

  if (s.includes('pending') || s.includes('deploying')) {
    return {
      icon: InProgressIcon,
      color: 'var(--pf-v6-global--info-color--100)',
      label: 'In Progress',
      className: 'pf-v6-u-info-color-100',
    };
  }

  if (s === 'deleted' || s.includes('not yet deployed')) {
    return {
      icon: MinusCircleIcon,
      color: 'var(--pf-v6-global--Color--200)',
      label: s === 'deleted' ? 'Deleted' : 'Not Yet Deployed',
      className: 'pf-v6-u-color-200',
    };
  }

  return {
    icon: QuestionCircleIcon,
    color: 'var(--pf-v6-global--Color--200)',
    label: 'Unknown',
    className: 'pf-v6-u-color-200',
  };
}
