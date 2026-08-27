import { zh } from '../../i18n/zh'
import { ChangePasswordForm } from '../../components/auth/ChangePasswordForm'
import { SettingsCard } from './SettingsPrimitives'

/** 设置页「修改密码」节（普通用户与管理员都可见）。 */
export function PasswordSection() {
  return (
    <SettingsCard
      title={zh.auth.changePassword.title}
      description={zh.auth.changePassword.subtitle}
    >
      <ChangePasswordForm />
    </SettingsCard>
  )
}
