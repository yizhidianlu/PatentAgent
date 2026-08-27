import { zh } from '../../i18n/zh'
import { SegmentedToggle } from '../ui/SegmentedToggle'
import { HOME_MODULES, MODULE_META, type HomeModule } from './moduleConfig'

export interface ModuleToggleProps {
  value: HomeModule
  onChange: (module: HomeModule) => void
  className?: string
}

/**
 * §2.4 / §3.1 ModuleToggle：四段（交底书 / 论文转专利 / 专利解读 / 审查答复）。
 * 位于 Composer 内卡底部工具栏最左侧（参考站实测：段钮 = 图标 + 简称，轨道 shrink-0），
 * 故用 zh.home.modulesShort 简称避免 764px 内卡挤压。
 * 持久化由 HomePage 的 changeModule 统一处理（ActivityPill 切换也走同一路径）。
 */
export function ModuleToggle({ value, onChange, className }: ModuleToggleProps) {
  return (
    <SegmentedToggle
      options={HOME_MODULES.map((m) => ({
        value: m,
        label: zh.home.modulesShort[m],
        icon: MODULE_META[m].icon,
      }))}
      value={value}
      onChange={onChange}
      ariaLabel={zh.home.moduleToggleLabel}
      className={className}
    />
  )
}
