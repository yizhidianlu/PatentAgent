import { WrenchScrewdriverIcon } from '@heroicons/react/24/outline'
import { zh } from '../i18n/zh'
import { EmptyState } from '../components/ui/EmptyState'

/** M1 占位页：居中 EmptyState「建设中」。 */
export function UnderConstruction({ title }: { title: string }) {
  return (
    <div className="flex-1 flex items-center justify-center">
      <EmptyState
        icon={WrenchScrewdriverIcon}
        title={`${title} · ${zh.common.underConstruction}`}
        description={zh.common.underConstructionDesc}
      />
    </div>
  )
}
