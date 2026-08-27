import { useNavigate } from 'react-router-dom'
import { QuestionMarkCircleIcon } from '@heroicons/react/24/outline'
import { zh } from '../i18n/zh'
import { EmptyState } from '../components/ui/EmptyState'
import { Button } from '../components/ui/Button'

export function NotFoundPage() {
  const navigate = useNavigate()
  return (
    <div className="flex-1 flex items-center justify-center">
      <EmptyState
        icon={QuestionMarkCircleIcon}
        title={zh.pages.notFoundTitle}
        description={zh.pages.notFoundDesc}
        action={
          <Button variant="secondary" onClick={() => navigate('/')}>
            {zh.pages.backHome}
          </Button>
        }
      />
    </div>
  )
}
