from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from product_decision_engine.domain.catalog import Catalog
from product_decision_engine.domain.models import (
    AvailabilityObservation,
    OfferAvailability,
    ProductLifecycleObservation,
    ProductLifecycleStatus,
    RetailerBasketAudit,
    UsageScenario,
    VerificationStatus,
)
from product_decision_engine.evaluation.retailer_baskets import (
    evaluate_retailer_basket,
)
from product_decision_engine.market import (
    FreshnessPolicy,
    audit_market_eligibility,
    availability_from_retailer_audits,
)


PROVIDER_GATE_STATUSES = frozenset({"pass", "fail", "unknown"})


@dataclass(frozen=True)
class ProviderReadinessSummary:
    provider_key: str
    display_name: str
    passed_gates: int
    required_gates: int
    production_ready: bool


@dataclass(frozen=True)
class CurrencyReconciliationSummary:
    sample_count: int
    within_tolerance_count: int
    tolerance_percent: Decimal
    maximum_error_percent: Decimal
    universally_reproducible: bool


def analyze_currency_reconciliation(
    reconciliation: dict[str, Any],
    cohort_ids: tuple[str, ...],
) -> CurrencyReconciliationSummary:
    rate = Decimal(reconciliation["usd_rub_rate"])
    tolerance = Decimal(reconciliation["tolerance_percent"])
    if rate <= 0 or tolerance < 0:
        raise ValueError("Currency rate must be positive and tolerance non-negative")

    samples = reconciliation["samples"]
    if not samples:
        raise ValueError("Currency reconciliation requires samples")
    sample_ids = tuple(item["product_id"] for item in samples)
    if len(set(sample_ids)) != len(sample_ids):
        raise ValueError("Currency reconciliation product ids must be unique")
    if not set(sample_ids) <= set(cohort_ids):
        raise ValueError("Currency reconciliation contains a product outside cohort")

    errors: list[Decimal] = []
    for sample in samples:
        usd_price = Decimal(sample["price_list_internet_usd"])
        rub_price = Decimal(sample["live_page_price_rub"])
        if usd_price <= 0 or rub_price <= 0:
            raise ValueError("Currency reconciliation prices must be positive")
        expected_rub = usd_price * rate
        errors.append(abs(rub_price - expected_rub) * 100 / expected_rub)

    within_tolerance = sum(error <= tolerance for error in errors)
    return CurrencyReconciliationSummary(
        sample_count=len(samples),
        within_tolerance_count=within_tolerance,
        tolerance_percent=tolerance,
        maximum_error_percent=max(errors),
        universally_reproducible=within_tolerance == len(samples),
    )


def analyze_provider_source_audit(
    provider_audit: dict[str, Any],
    cohort_ids: tuple[str, ...],
) -> tuple[ProviderReadinessSummary, ...]:
    if provider_audit["cohort_size"] != len(cohort_ids):
        raise ValueError("Provider audit cohort size does not match the cohort")
    if provider_audit["counts_toward_m3_snapshot"]:
        raise ValueError("Source feasibility audit must not impersonate an M3 snapshot")

    required_gates = tuple(provider_audit["required_readiness_gates"])
    if not required_gates or len(set(required_gates)) != len(required_gates):
        raise ValueError("Provider readiness gates must be non-empty and unique")

    summaries: list[ProviderReadinessSummary] = []
    provider_keys: set[str] = set()
    for provider in provider_audit["providers"]:
        provider_key = provider["provider_key"]
        if provider_key in provider_keys:
            raise ValueError(f"Duplicate provider key: {provider_key}")
        provider_keys.add(provider_key)

        gates = provider["readiness_gates"]
        if set(gates) != set(required_gates):
            raise ValueError(
                f"Provider {provider_key} must declare every readiness gate"
            )
        invalid_statuses = set(gates.values()) - PROVIDER_GATE_STATUSES
        if invalid_statuses:
            raise ValueError(
                f"Provider {provider_key} has invalid gate statuses: "
                f"{sorted(invalid_statuses)}"
            )

        coverage = provider.get("cohort_coverage")
        if coverage is not None:
            device_rows = coverage["exact_device_rows"]
            matched = set(device_rows["matched_product_ids"])
            not_matched = set(device_rows["not_matched_product_ids"])
            if matched & not_matched or matched | not_matched != set(cohort_ids):
                raise ValueError(
                    f"Provider {provider_key} device coverage must partition the cohort"
                )
            live_ids = {
                item["product_id"]
                for item in provider.get("live_product_page_checks", [])
            }
            if live_ids and live_ids != set(cohort_ids):
                raise ValueError(
                    f"Provider {provider_key} live page checks must cover the cohort"
                )
            consumables = coverage["exact_oem_consumables"]
            if consumables["exact_parts_matched"] > consumables[
                "required_parts_tested"
            ]:
                raise ValueError(
                    f"Provider {provider_key} matched more OEM parts than tested"
                )
            if consumables["complete_product_sets"] > len(cohort_ids):
                raise ValueError(
                    f"Provider {provider_key} has too many complete OEM sets"
                )
        passed = sum(value == "pass" for value in gates.values())
        summaries.append(
            ProviderReadinessSummary(
                provider_key=provider_key,
                display_name=provider["display_name"],
                passed_gates=passed,
                required_gates=len(required_gates),
                production_ready=passed == len(required_gates),
            )
        )

    if not summaries:
        raise ValueError("Provider source audit must contain at least one provider")
    return tuple(summaries)


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _rub(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


def _percent(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0,0%"
    return f"{numerator * 100 / denominator:.1f}%".replace(".", ",")


def _device_check_availability(
    snapshot: dict[str, Any],
) -> tuple[AvailabilityObservation, ...]:
    state_map = {
        "current_offers": OfferAvailability.IN_STOCK,
        "no_current_offers": OfferAvailability.UNAVAILABLE,
        "not_verified": OfferAvailability.UNVERIFIED,
    }
    return tuple(
        AvailabilityObservation(
            id=f"{snapshot['id']}:{item['product_id']}",
            product_id=item["product_id"],
            source_provider_key=snapshot["provider"]["provider_key"],
            availability=state_map[item["status"]],
            observed_at=snapshot["observed_at"],
            verification_status=VerificationStatus(item["verification_status"]),
            source_url=item["source_url"],
        )
        for item in snapshot["device_checks"]
    )


def _lifecycle_observations(
    snapshot: dict[str, Any],
) -> tuple[ProductLifecycleObservation, ...]:
    return tuple(
        ProductLifecycleObservation(
            id=f"{snapshot['id']}:lifecycle:{item['product_id']}",
            product_id=item["product_id"],
            source_provider_key=snapshot["provider"]["provider_key"],
            lifecycle_status=ProductLifecycleStatus(item["lifecycle_status"]),
            observed_at=snapshot["observed_at"],
            verification_status=VerificationStatus(item["verification_status"]),
            source_url=item["source_url"],
            notes=item["notes"],
        )
        for item in snapshot["lifecycle_checks"]
    )


def build_phase1_market_report(
    catalog: Catalog,
    scenarios: tuple[UsageScenario, ...],
    cohort: list[dict[str, Any]],
    snapshot: dict[str, Any],
    provider_audit: dict[str, Any],
    golden_audits: tuple[RetailerBasketAudit, ...],
    price_ru_audits: tuple[RetailerBasketAudit, ...],
) -> str:
    cohort_ids = tuple(item["product_id"] for item in cohort)
    if len(cohort_ids) < 12:
        raise ValueError("Phase 1 cohort must contain at least 12 products")
    if len(set(cohort_ids)) != len(cohort_ids):
        raise ValueError("Phase 1 cohort product ids must be unique")
    for product_id in cohort_ids:
        catalog.product(product_id)
    provider_summaries = analyze_provider_source_audit(
        provider_audit, cohort_ids
    )

    as_of = date.fromisoformat(snapshot["observed_at"])
    policy = FreshnessPolicy()
    all_audits = (*golden_audits, *price_ru_audits)
    availability = (
        *availability_from_retailer_audits(all_audits),
        *_device_check_availability(snapshot),
    )
    lifecycle = _lifecycle_observations(snapshot)
    market_audits = tuple(
        audit_market_eligibility(
            catalog,
            catalog.product(product_id),
            availability,
            lifecycle,
            as_of=as_of,
            policy=policy,
        )
        for product_id in cohort_ids
    )

    device_checks = snapshot["device_checks"]
    current_offer_checks = tuple(
        item
        for item in device_checks
        if item["verification_status"] == "verified"
        and item["status"] == "current_offers"
    )
    no_offer_checks = tuple(
        item
        for item in device_checks
        if item["verification_status"] == "verified"
        and item["status"] == "no_current_offers"
    )
    unverified_checks = tuple(
        item for item in device_checks if item["verification_status"] != "verified"
    )

    fresh_complete_product_ids = {
        offer.product_id
        for audit in all_audits
        if audit.verification_status == VerificationStatus.VERIFIED
        and 0 <= (as_of - date.fromisoformat(audit.observed_at)).days <= 7
        for offer in audit.offers
        if offer.complete
    } & set(cohort_ids)

    market_eligible = tuple(item for item in market_audits if item.eligible)
    blockers = Counter(
        reason.split(":", 1)[0]
        for item in market_audits
        for reason in item.blocking_reasons
    )

    scenario = next(
        item for item in scenarios if item.id == price_ru_audits[0].scenario_id
    )
    current_result = evaluate_retailer_basket(
        catalog, scenario, price_ru_audits[0]
    )
    prior_regard = next(
        item
        for item in golden_audits
        if item.id == "home-light-pair-regard-20260820"
    )
    prior_result = evaluate_retailer_basket(catalog, scenario, prior_regard)

    lines = [
        "# Фаза 1 — проверка российского рынка и свежести данных",
        "",
        f"Срез: **{snapshot['observed_at']}**, рынок: **Россия / Москва / RUB**.",
        "",
        "## Промежуточное решение",
        "",
        "Статус: **`CONTINUE PHASE 1 / M2–M3`**. Price.ru полезен как агрегатор "
        "цен и наличия, но публичные страницы нельзя превращать в production-scraper. "
        "Для production проверяется официальный партнёрский XML-фид. Текущий срез "
        "ещё не достигает порога полной корзины и не даёт `GO` к web MVP.",
        "",
        "## Когорта и покрытие первого среза",
        "",
        f"- Моделей в когорте: **{len(cohort_ids)}**.",
        f"- Карточек с подтверждёнными текущими предложениями Price.ru: "
        f"**{len(current_offer_checks)} / {len(cohort_ids)} "
        f"({_percent(len(current_offer_checks), len(cohort_ids))})**.",
        f"- Карточек без текущих предложений: **{len(no_offer_checks)} / {len(cohort_ids)}**.",
        f"- Точных карточек, не подтверждённых этой ручной проверкой: "
        f"**{len(unverified_checks)} / {len(cohort_ids)}**.",
        f"- Полных свежих корзин устройства и OEM-расходников: "
        f"**{len(fresh_complete_product_ids)} / {len(cohort_ids)} "
        f"({_percent(len(fresh_complete_product_ids), len(cohort_ids))})**.",
        f"- Моделей, прошедших формальный freshness/lifecycle gate: "
        f"**{len(market_eligible)} / {len(cohort_ids)} "
        f"({_percent(len(market_eligible), len(cohort_ids))})**.",
        "",
        "Ключевое различие: наличие карточки или даже устройства в продаже ещё не "
        "означает наличие полной OEM-корзины для воспроизводимого TCO.",
        "",
        "## Результат freshness/lifecycle gate",
        "",
        f"Политика среза: цена не старше **{policy.price_max_age_days} дней**, "
        f"наличие не старше **{policy.availability_max_age_days} дней**.",
        "",
        "| Модель | Допуск | Причины блокировки | Предупреждения |",
        "|---|---:|---|---|",
    ]
    for item in market_audits:
        product = catalog.product(item.product_id)
        lines.append(
            f"| {product.manufacturer} {product.model} | "
            f"{'да' if item.eligible else 'нет'} | "
            f"{'; '.join(item.blocking_reasons) or '—'} | "
            f"{'; '.join(item.warnings) or '—'} |"
        )

    lines.extend(
        [
            "",
            "Сводка причин блокировки: "
            + (", ".join(f"`{key}` — {value}" for key, value in sorted(blockers.items())) or "нет")
            + ".",
            "",
            "`lifecycle_unknown` является предупреждением, а не выдуманным выводом о "
            "снятии с производства. Свежая цена и наличие могут временно допустить модель; "
            "подтверждённый `discontinued` всегда блокирует её.",
            "",
            "## Повторная цена прямой картриджной пары",
            "",
            f"Сценарий: **{scenario.name}**. Сравнивается одна корзина продавца "
            "Regard, найденная через Price.ru; совместимые картриджи исключены по exact "
            "OEM part number.",
            "",
            "| Показатель | Предыдущая проверка Regard | Price.ru → Regard |",
            "|---|---:|---:|",
            f"| Цена Canon PIXMA TS3640 | "
            f"{_rub(prior_result.product_result('canon-pixma-ts3640').full_tco.purchase_cost_rub)} | "
            f"{_rub(current_result.product_result('canon-pixma-ts3640').full_tco.purchase_cost_rub)} |",
            f"| TCO Canon PIXMA TS3640 | "
            f"{_rub(prior_result.product_result('canon-pixma-ts3640').full_tco.total_cost_rub)} | "
            f"{_rub(current_result.product_result('canon-pixma-ts3640').full_tco.total_cost_rub)} |",
            f"| Цена HP DeskJet 2875 | "
            f"{_rub(prior_result.product_result('hp-deskjet-ink-advantage-2875').full_tco.purchase_cost_rub)} | "
            f"{_rub(current_result.product_result('hp-deskjet-ink-advantage-2875').full_tco.purchase_cost_rub)} |",
            f"| TCO HP DeskJet 2875 | "
            f"{_rub(prior_result.product_result('hp-deskjet-ink-advantage-2875').full_tco.total_cost_rub)} | "
            f"{_rub(current_result.product_result('hp-deskjet-ink-advantage-2875').full_tco.total_cost_rub)} |",
            "",
            f"Предыдущий baseline по цене покупки выбирал **{prior_result.purchase_price_winner.product.manufacturer} "
            f"{prior_result.purchase_price_winner.product.model}**, а Decision Engine — "
            f"**{prior_result.decision_engine_winner.product.manufacturer} "
            f"{prior_result.decision_engine_winner.product.model}**. В новом срезе и baseline, "
            f"и TCO выбирают **{current_result.decision_engine_winner.product.manufacturer} "
            f"{current_result.decision_engine_winner.product.model}**, потому что HP теперь "
            "дешевле уже при покупке. Это нормальная смена результата из-за цены, а не "
            "ошибка движка.",
            "",
            "## Проверка источника",
            "",
            f"- Условия публичных страниц: {snapshot['provider']['terms_url']}",
            f"- Официальный путь к XML-фиду: {snapshot['provider']['partner_program_url']}",
            "- Публичная автоматическая выгрузка без разрешения: **restricted**.",
            "- Роль Price.ru: цена и наличие; technical facts и yields остаются за "
            "официальными источниками производителей.",
            "- Минимум карточки расходника нельзя брать автоматически: в одной карточке "
            "могут смешиваться OEM и совместимые предложения.",
            "",
            "## Проверка специализированных российских источников",
            "",
            f"Дата проверки: **{provider_audit['observed_at']}**. Это расширение "
            "источников **не считается вторым M3-срезом**: между проверками прошёл "
            "только один день, а контракт требует общий интервал не менее 14 дней.",
            "",
            "Production-ready означает прохождение всех семи обязательных гейтов без "
            "взвешенного или магического score: структурированный доступ, exact product, "
            "exact OEM, цена в RUB, наличие, стабильный идентификатор и подтверждённые "
            "права коммерческой автоматизации.",
            "",
            "| Источник | Пройдено гейтов | Production-ready | Проверенная роль |",
            "|---|---:|---:|---|",
        ]
    )
    provider_by_key = {
        item["provider_key"]: item for item in provider_audit["providers"]
    }
    for summary in provider_summaries:
        provider = provider_by_key[summary.provider_key]
        lines.append(
            f"| {summary.display_name} | {summary.passed_gates} / "
            f"{summary.required_gates} | "
            f"{'да' if summary.production_ready else 'нет'} | "
            f"`{provider['tested_role']}` |"
        )

    kns = provider_by_key["kns"]
    price_ru = provider_by_key["price-ru"]
    kns_coverage = kns["cohort_coverage"]
    kns_device_matches = len(
        kns_coverage["exact_device_rows"]["matched_product_ids"]
    )
    kns_consumables = kns_coverage["exact_oem_consumables"]
    currency_reconciliation = analyze_currency_reconciliation(
        kns["currency_reconciliation"], cohort_ids
    )
    maximum_error_display = (
        f"{currency_reconciliation.maximum_error_percent:.1f}".replace(".", ",")
    )
    conflict = provider_audit["cross_source_conflicts"][0]
    lines.extend(
        [
            "",
            "### Что дал KNS",
            "",
            f"Официальный XLS-прайс KNS от **{kns['official_price_list']['created_at']}** "
            f"содержит **{kns['official_price_list']['row_count']} строк**. Exact device "
            f"найден для **{kns_device_matches} / {len(cohort_ids)} "
            f"({_percent(kns_device_matches, len(cohort_ids))})** моделей, а exact OEM "
            f"расходники — **{kns_consumables['exact_parts_matched']} / "
            f"{kns_consumables['required_parts_tested']}** обязательных позиций, то есть "
            f"полные наборы расходников есть для **{kns_consumables['complete_product_sets']} / "
            f"{len(cohort_ids)}** моделей.",
            "",
            "Это заметный прогресс в обнаружении exact SKU, но ещё не полная текущая "
            "корзина. В файле нет явного наличия, стабильного item id/URL и правила "
            "пересчёта USD-строк в RUB. Поэтому из одного XLS нельзя честно получить ни "
            "production-цену, ни допуск модели к рекомендации.",
            "",
            "Карточки KNS отдельно подтверждают, что источник умеет давать exact MPN, "
            "RUB-цену и состояние товара. Однако соединение прайс-листа с карточкой пока "
            "потребовало бы эвристического поиска по названию — это не принимается как "
            "устойчивый provider adapter без feed или стабильного ключа.",
            "",
            "Проверка прямого пересчёта по официальному курсу ЦБ также не закрыла "
            "проблему. В пределах опубликованного KNS порога **"
            f"{str(currency_reconciliation.tolerance_percent).replace('.', ',')}%** "
            f"оказались **{currency_reconciliation.within_tolerance_count} / "
            f"{currency_reconciliation.sample_count}** доступных устройств; максимальное "
            "отклонение составило **"
            f"{maximum_error_display}%**. Единый курс "
            "не воспроизводит промо-цены, поэтому финальная RUB-цена должна приходить "
            "из карточки или договорного feed как отдельное поле.",
            "",
            "### Что подтвердилось по XML Price.ru",
            "",
            "Price.ru публично подтверждает исходящий affiliate XML после заявки: feed "
            "охватывает все товары и содержит ссылки и описания. Но цена, seller, "
            "availability, timestamp и exact MPN в публичном описании не заявлены. "
            f"Неподтверждённых обязательных полей: **"
            f"{len(price_ru['affiliate_program_check']['fields_not_publicly_confirmed'])}**. "
            "До получения образца и договора этот канал остаётся кандидатом, а не "
            "production-approved источником TCO.",
            "",
            "### Обнаруженный конфликт между источниками",
            "",
            f"- **Epson EcoTank L4260**: {conflict['source_a']} "
            f"{conflict['source_b']} Конфликт оставлен `unresolved`; действие: "
            f"{conflict['required_action']}",
            "",
            "ForOffice полезен как точная ручная карточка: Brother DCP-T520W найден по "
            "DCPT520WR1, но устройство только под заказ, а из четырёх обязательных OEM-"
            "флаконов в карточке перечислены три. Принтер-Плоттер.ру полезен как "
            "независимый lifecycle-сигнал: Canon G1411 прямо помечен архивным, а для "
            "Epson L4260 предлагается запросить цену. Ни один из этих источников пока не "
            "дал публичный структурированный feed.",
            "",
            "Вывод проверки: **KNS — основной кандидат для следующего due diligence** "
            "структурированного каталога, а affiliate XML Price.ru — второй официальный "
            "кандидат на предложения. Профильные магазины остаются независимой проверкой "
            "exact SKU, наличия и lifecycle. Ни один источник ещё не получил статус "
            "production-approved.",
            "",
            "Готовые запросы образцов feed и договорных условий сохранены в "
            "`docs/provider-access-request.md`; они не отправляются от имени владельца "
            "проекта без отдельного разрешения.",
            "",
            "## Что проверяется дальше",
            "",
            "1. Запросить у KNS dealer/export feed с RUB, availability, stable item id/URL "
            "и явными условиями коммерческого использования.",
            "2. Параллельно получить условия партнёрского XML-фида Price.ru и проверить "
            "exact SKU, seller, price и availability.",
            "3. Добрать полные OEM-корзины минимум до 80% когорты или честно сузить когорту.",
            "4. Выполнить ещё два среза в разные дни так, чтобы общий интервал составил "
            "не менее 14 дней.",
            "5. Учитывать фактическое ручное время и специальные исключения начиная со "
            "следующего среза; для текущего ретроспективного среза время не выдумывается.",
            "",
            "## Риски",
            "",
            "- На первом срезе полная свежая корзина существенно ниже целевых 80%; это "
            "главный текущий риск, но ещё не финальный провал M3.",
            "- Исчезновение предложения нельзя путать со снятием модели с производства.",
            "- Без exact MPN/part number агрегатор может вернуть совместимый расходник как "
            "самый дешёвый и занизить TCO.",
            "- Если официальный feed недоступен на приемлемых условиях, вариант со scraping "
            "публичных страниц не рассматривается как устойчивое production-решение.",
            "- Конфликт Epson L4260 показывает, что даже предложение агрегатора с именем "
            "продавца нельзя считать подтверждённым наличием без direct seller URL и exact MPN.",
            "",
        ]
    )
    return "\n".join(lines)
