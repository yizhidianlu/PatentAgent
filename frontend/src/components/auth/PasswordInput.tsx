import { forwardRef, useState, type InputHTMLAttributes } from 'react'
import { EyeIcon, EyeSlashIcon } from '@heroicons/react/24/outline'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { Input } from '../ui/Input'

export type PasswordInputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'type'>

/** 密码输入框：右侧眼睛切换明文；登录与改密两处共用。 */
export const PasswordInput = forwardRef<HTMLInputElement, PasswordInputProps>(
  function PasswordInput({ className, ...rest }, ref) {
    const [visible, setVisible] = useState(false)
    const Icon = visible ? EyeSlashIcon : EyeIcon
    return (
      <div className="relative">
        <Input
          ref={ref}
          type={visible ? 'text' : 'password'}
          spellCheck={false}
          className={cn('pr-11', className)}
          {...rest}
        />
        <button
          type="button"
          tabIndex={-1}
          onClick={() => setVisible((v) => !v)}
          aria-label={visible ? zh.auth.login.hidePassword : zh.auth.login.showPassword}
          className="absolute right-2 top-1/2 -translate-y-1/2 w-7 h-7 rounded-lg flex items-center justify-center text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
        >
          <Icon className="w-4 h-4" strokeWidth={1.5} />
        </button>
      </div>
    )
  },
)
